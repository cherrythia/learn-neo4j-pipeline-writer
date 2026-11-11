from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from demo_env.samples import PDF_PATH, XLSX_PATH
from writer_service.ingestion.documents import (
    EXTRACTION_PROMPT,
    DocumentExtractor,
    SourceChunk,
    pdf_chunks,
    render_chunks,
    windows,
    xlsx_chunks,
)
from writer_service.llm.base import LLMOutputError
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation

ROOT = Path(__file__).resolve().parents[2]
SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)


# ------------------------------------------------------------------ parsing


def test_pdf_chunks_one_per_page_with_anchors() -> None:
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="aurora.pdf")
    assert [c.locator for c in chunks] == ["page 1", "page 2"]
    assert all(c.source_type == "pdf" and c.source_ref == "aurora.pdf" for c in chunks)
    assert "LR-705" in chunks[0].text and "ESG" in chunks[1].text


def test_pdf_chunks_accept_bytes() -> None:
    data = (ROOT / PDF_PATH).read_bytes()
    assert len(pdf_chunks(data, source_ref="upload.pdf")) == 2


def test_xlsx_chunks_one_per_row_with_header_names() -> None:
    chunks = xlsx_chunks(ROOT / XLSX_PATH, source_ref="checklist.xlsx")
    first = chunks[0]
    assert first.source_type == "excel"
    assert first.locator == "sheet Checklist row 2"  # row 1 is the header
    assert first.text.startswith("issuer: Aurora Pacific Holdings (Demo); rule_code: LR-705")
    assert [c.locator for c in chunks] == [f"sheet Checklist row {n}" for n in range(2, 14)]


def test_unsupported_bytes_raise_clear_error() -> None:
    with pytest.raises(ValueError, match="not a readable PDF"):
        pdf_chunks(b"not a pdf", source_ref="x.pdf")
    with pytest.raises(ValueError, match="not a readable Excel workbook"):
        xlsx_chunks(b"not a workbook", source_ref="x.xlsx")


def test_windows_group_chunks_under_limit() -> None:
    cs = [SourceChunk("pdf", "a", f"page {i}", "x" * 40) for i in range(1, 6)]
    groups = windows(cs, max_chars=100)
    assert [[c.locator for c in g] for g in groups] == [
        ["page 1", "page 2"],
        ["page 3", "page 4"],
        ["page 5"],
    ]
    big = [SourceChunk("pdf", "a", "page 1", "y" * 500)]
    assert windows(big, max_chars=100) == [big]  # oversized chunk stays whole


def test_render_marks_each_chunk() -> None:
    text = render_chunks(
        [SourceChunk("pdf", "a", "page 1", "Hello"), SourceChunk("pdf", "a", "page 2", "World")]
    )
    assert text == "[page 1]\nHello\n\n[page 2]\nWorld"


# ------------------------------------------------------------------ ontology-constrained schema


def test_output_schema_restricts_names_to_published_ontology() -> None:
    deprecated = OntologySpec(
        tuple(replace(c, deprecated=True) if c.name == "Finding" else c for c in CORE_CLASSES),
        tuple(
            replace(r, deprecated=True) if "Finding" in (r.domain, r.range) else r
            for r in CORE_RELATIONS
        ),
    )
    ex = DocumentExtractor(
        StructuredLLM(FakeLLM(), InMemoryCache()), deprecated, ontology_version=2
    )
    schema = ex.output_model().model_json_schema()
    text = str(schema)
    assert "Issuer" in text and "FILED_BY" in text
    assert "'Finding'" not in text and "CONCERNS" not in text  # deprecated: not offered


# ------------------------------------------------------------------ extraction

PAGE_EVIDENCE = {"locator": "page 1", "quote": "Aurora Pacific Holdings (Demo) (ticker APH)"}


def answer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entities": [
            {
                "class_name": "Issuer",
                "properties": [
                    {"name": "ticker", "value": "APH"},
                    {"name": "name", "value": "Aurora Pacific Holdings (Demo)"},
                ],
                "evidence": [PAGE_EVIDENCE, {"locator": "page 99", "quote": "made up"}],
                "confidence": 0.9,
            }
        ],
        "relations": [
            {
                "relation": "FILED_BY",
                "source_class": "Filing",
                "source_key": "AR-2025-APH",
                "target_class": "Issuer",
                "target_key": "APH",
                "evidence": [{"locator": "page 1", "quote": "issued this annual report"}],
                "confidence": 0.8,
            }
        ],
    }
    base.update(overrides)
    return base


def extractor(fake: FakeLLM) -> DocumentExtractor:
    return DocumentExtractor(StructuredLLM(fake, InMemoryCache()), SPEC, ontology_version=1)


def test_extract_converts_to_candidates_with_full_evidence() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer()]})
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="aurora.pdf")
    cands = extractor(fake).extract(chunks)
    ent = next(c for c in cands if isinstance(c, CandidateEntity))
    rel = next(c for c in cands if isinstance(c, CandidateRelation))
    assert ent.properties == {"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"}
    assert [(e.source_type, e.source_ref, e.locator) for e in ent.evidence] == [
        ("pdf", "aurora.pdf", "page 1")  # page 99 does not exist: dropped
    ]
    assert (rel.source.key, rel.target.key, rel.confidence) == ("AR-2025-APH", "APH", 0.8)


def test_prompt_contains_ontology_and_page_markers() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer()]})
    extractor(fake).extract(pdf_chunks(ROOT / PDF_PATH, source_ref="aurora.pdf"))
    (req,) = fake.calls  # both pages fit one window
    assert "[page 1]" in req.user and "[page 2]" in req.user
    assert "Issuer (key: ticker" in req.user and "(Filing)-[:FILED_BY]->(Issuer)" in req.user
    assert "aurora.pdf" in req.user


def test_samples_are_distinct_calls() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer(), answer(relations=[])]})
    ex = extractor(fake)
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="aurora.pdf")
    first, second = ex.extract(chunks, sample=0), ex.extract(chunks, sample=1)
    assert len(fake.calls) == 2 and len(first) == 2 and len(second) == 1


def test_off_vocabulary_answer_is_rejected_by_schema() -> None:
    bad = answer(
        entities=[{"class_name": "Company", "properties": [], "evidence": [], "confidence": 1.0}]
    )
    with pytest.raises(LLMOutputError):
        extractor(FakeLLM({EXTRACTION_PROMPT.name: [bad]})).extract(
            pdf_chunks(ROOT / PDF_PATH, source_ref="aurora.pdf")
        )


def test_each_window_is_a_separate_call() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer(relations=[])]})
    chunks = [SourceChunk("pdf", "a.pdf", f"page {i}", "text " * 300) for i in range(1, 4)]
    ex = DocumentExtractor(
        StructuredLLM(fake, InMemoryCache()), SPEC, ontology_version=1, max_chars=2000
    )
    ex.extract(chunks)
    assert len(fake.calls) == 3
