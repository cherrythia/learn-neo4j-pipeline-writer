"""PDF and Excel ingestion: parse into anchored chunks, then extract candidate facts with an
LLM that can only use the published ontology's vocabulary (ADR-0007).

Anchors ("page 2", "sheet Checklist row 5") flow into every piece of evidence, so the
verification step can check each quote against the exact page or row it claims to come from.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openpyxl import load_workbook
from pydantic import BaseModel, Field, create_model
from pypdf import PdfReader

from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
    SourceType,
    canonical_text,
)

AnyCandidate = CandidateEntity | CandidateRelation
DEFAULT_WINDOW_CHARS = 12_000


@dataclass(frozen=True)
class SourceChunk:
    source_type: SourceType
    source_ref: str
    locator: str
    text: str


# ------------------------------------------------------------------------- parsing


def _open(source: Path | bytes) -> io.BytesIO:
    return io.BytesIO(source if isinstance(source, bytes) else Path(source).read_bytes())


def pdf_chunks(source: Path | bytes, *, source_ref: str) -> list[SourceChunk]:
    """One chunk per non-empty page, locator ``page N`` (1-based)."""
    try:
        reader = PdfReader(_open(source))
        pages = [p.extract_text() or "" for p in reader.pages]
    except Exception as exc:  # pypdf raises many unrelated types on corrupt input
        # (PdfReadError, LimitReachedError, NotImplementedError for unsupported filters, …)
        raise ValueError(f"{source_ref} is not a readable PDF: {exc}") from exc
    return [
        SourceChunk("pdf", source_ref, f"page {i}", text.strip())
        for i, text in enumerate(pages, start=1)
        if text.strip()
    ]


def xlsx_chunks(source: Path | bytes, *, source_ref: str) -> list[SourceChunk]:
    """One chunk per non-empty data row, ``header: value; …``; locator ``sheet S row N``
    (row numbers as Excel shows them, so row 1 is the header)."""
    try:
        wb = load_workbook(_open(source), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types for bad input
        raise ValueError(f"{source_ref} is not a readable Excel workbook: {exc}") from exc
    chunks: list[SourceChunk] = []
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(rows, ())]
            for row_no, row in enumerate(rows, start=2):
                cells = [
                    f"{header[i] or f'column {i + 1}'}: {canonical_text(str(v))}"
                    for i, v in enumerate(row)
                    if v is not None and str(v).strip() and i < len(header)
                ]
                if cells:
                    chunks.append(
                        SourceChunk(
                            "excel", source_ref, f"sheet {ws.title} row {row_no}", "; ".join(cells)
                        )
                    )
    except Exception as exc:  # read-only mode parses sheet XML lazily, during iteration
        raise ValueError(f"{source_ref} is not a readable Excel workbook: {exc}") from exc
    finally:
        wb.close()
    return chunks


def windows(
    chunks: list[SourceChunk], max_chars: int = DEFAULT_WINDOW_CHARS
) -> list[list[SourceChunk]]:
    """Group consecutive chunks into LLM-sized windows; a chunk is never split."""
    groups: list[list[SourceChunk]] = []
    current: list[SourceChunk] = []
    size = 0
    for chunk in chunks:
        if current and size + len(chunk.text) > max_chars:
            groups.append(current)
            current, size = [], 0
        current.append(chunk)
        size += len(chunk.text)
    if current:
        groups.append(current)
    return groups


def render_chunks(chunks: list[SourceChunk]) -> str:
    return "\n\n".join(f"[{c.locator}]\n{c.text}" for c in chunks)


# ------------------------------------------------------------------------- extraction


def render_ontology(spec: OntologySpec) -> str:
    """The vocabulary the extractor may use (active classes and relations only)."""
    lines = ["Classes (* = required):"]
    for c in spec.active_classes():
        props = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in c.properties)
        parent = f" is-a {c.parent}" if c.parent else ""
        lines.append(f"- {c.name} (key: {c.identity_key}; properties: {props}){parent}")
    lines.append("Relations:")
    for r in spec.relations:
        if not r.deprecated:
            lines.append(f"- ({r.domain})-[:{r.name}]->({r.range}) {r.cardinality}")
    return "\n".join(lines)


EXTRACTION_PROMPT = PromptTemplate(
    name="document_extraction",
    version=1,
    system=(
        "You extract facts for a compliance knowledge graph. Use ONLY the classes, properties "
        "and relations in the ontology you are given. Extract only what the text states; never "
        "infer or use outside knowledge. Every fact must cite evidence: the exact marker of the "
        "chunk it comes from (e.g. 'page 2') and a short quote copied verbatim from that chunk. "
        "Refer to entities in relations by their identity key value when the text gives it."
    ),
    user=(
        "Ontology:\n{ontology}\n\nDocument: {source_ref}\n\n{text}\n\n"
        "Return the entities and relations stated in this text, each with evidence and a "
        "confidence between 0 and 1."
    ),
)


class _Evidence(BaseModel):
    locator: str
    quote: str


class _Property(BaseModel):
    name: str
    value: str


class DocumentExtractor:
    def __init__(
        self,
        llm: StructuredLLM,
        spec: OntologySpec,
        ontology_version: int,
        max_chars: int = DEFAULT_WINDOW_CHARS,
    ) -> None:
        self.llm = llm
        self.spec = spec
        self.ontology_version = ontology_version
        self.max_chars = max_chars
        self._model = self._build_output_model()

    def output_model(self) -> type[BaseModel]:
        return self._model

    def _build_output_model(self) -> type[BaseModel]:
        """Pydantic model whose JSON schema enumerates the ontology's names, so structured
        output can't produce a class, relation or property outside the published vocabulary."""
        classes = tuple(c.name for c in self.spec.active_classes())
        relations = tuple(r.name for r in self.spec.relations if not r.deprecated)
        prop_names = tuple(
            sorted({p.name for c in self.spec.active_classes() for p in c.properties})
        )
        ClassName: Any = Literal[classes]
        PropName: Any = Literal[prop_names]
        Prop = create_model("ExtractedProperty", name=(PropName, ...), value=(str, ...))
        Entity = create_model(
            "ExtractedEntity",
            class_name=(ClassName, ...),
            properties=(list[Prop], ...),
            evidence=(list[_Evidence], ...),
            confidence=(float, Field(ge=0, le=1)),
        )
        fields: dict[str, Any] = {"entities": (list[Entity], ...)}
        if relations:
            RelName: Any = Literal[relations]
            Relation = create_model(
                "ExtractedRelation",
                relation=(RelName, ...),
                source_class=(ClassName, ...),
                source_key=(str, ...),
                target_class=(ClassName, ...),
                target_key=(str, ...),
                evidence=(list[_Evidence], ...),
                confidence=(float, Field(ge=0, le=1)),
            )
            fields["relations"] = (list[Relation], ...)
        model: type[BaseModel] = create_model("Extraction", **fields)
        return model

    def extract(self, chunks: list[SourceChunk], sample: int = 0) -> list[AnyCandidate]:
        """Candidates from all windows of the document (one LLM call per window)."""
        out: list[AnyCandidate] = []
        for window in windows(chunks, self.max_chars):
            if not window:
                continue
            first = window[0]
            result = self.llm.ask(
                EXTRACTION_PROMPT,
                {
                    "ontology": render_ontology(self.spec),
                    "source_ref": first.source_ref,
                    "text": render_chunks(window),
                },
                self._model,
                ontology_version=self.ontology_version,
                sample=sample,
            )
            out.extend(self._convert(result.value.model_dump(), window))
        return out

    @staticmethod
    def _evidence(items: list[dict[str, Any]], window: list[SourceChunk]) -> list[Evidence]:
        """Keep only evidence citing a chunk that exists in this window."""
        # Compare canonical forms on both sides: a sheet titled "Two  Spaces" must still match.
        known = {canonical_text(c.locator): c for c in window}
        out = []
        for e in items:
            chunk = known.get(canonical_text(e["locator"]).strip("[]"))
            if chunk is not None and e["quote"].strip():
                out.append(
                    Evidence(
                        source_type=chunk.source_type,
                        source_ref=chunk.source_ref,
                        locator=chunk.locator,
                        quote=e["quote"].strip(),
                    )
                )
        return out

    def _convert(self, data: dict[str, Any], window: list[SourceChunk]) -> list[AnyCandidate]:
        out: list[AnyCandidate] = []
        for e in data.get("entities", []):
            out.append(
                CandidateEntity(
                    class_name=e["class_name"],
                    properties={p["name"]: p["value"] for p in e["properties"]},
                    evidence=self._evidence(e["evidence"], window),
                    confidence=e["confidence"],
                )
            )
        for r in data.get("relations", []):
            out.append(
                CandidateRelation(
                    relation=r["relation"],
                    source=EntityRef(class_name=r["source_class"], key=r["source_key"]),
                    target=EntityRef(class_name=r["target_class"], key=r["target_key"]),
                    evidence=self._evidence(r["evidence"], window),
                    confidence=r["confidence"],
                )
            )
        return out
