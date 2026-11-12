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
    render_ontology,
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
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf")
    assert [c.locator for c in chunks] == ["page 1", "page 2", "page 3"]
    assert all(c.source_type == "pdf" and c.source_ref == "invoice.pdf" for c in chunks)
    assert "HW-25-0101" in chunks[0].text and "SL-25-0201" in chunks[1].text
    assert "is assigned to Finance (FIN)" in chunks[2].text


def test_pdf_chunks_accept_bytes() -> None:
    data = (ROOT / PDF_PATH).read_bytes()
    assert len(pdf_chunks(data, source_ref="upload.pdf")) == 3


def test_xlsx_chunks_one_per_row_with_header_names() -> None:
    from demo_env.data import HARDWARE

    chunks = xlsx_chunks(ROOT / XLSX_PATH, source_ref="register.xlsx")
    first = chunks[0]
    assert first.source_type == "excel"
    assert first.locator == "sheet Register row 2"  # row 1 is the header
    assert first.text.startswith(f"asset_tag: {HARDWARE[0].asset_tag}; hardware_type: laptop")
    assert [c.locator for c in chunks] == [
        f"sheet Register row {n}" for n in range(2, len(HARDWARE) + 2)
    ]


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
        tuple(replace(c, deprecated=True) if c.name == "Application" else c for c in CORE_CLASSES),
        tuple(
            replace(r, deprecated=True) if "Application" in (r.domain, r.range) else r
            for r in CORE_RELATIONS
        ),
    )
    ex = DocumentExtractor(
        StructuredLLM(FakeLLM(), InMemoryCache()), deprecated, ontology_version=2
    )
    schema = ex.output_model().model_json_schema()
    text = str(schema)
    assert "HardwareAsset" in text and "ASSIGNED_TO" in text
    assert "'Application'" not in text and "OWNED_BY" not in text  # deprecated: not offered


# ------------------------------------------------------------------ extraction

PAGE_EVIDENCE = {"locator": "page 1", "quote": "HW-25-0101 Laptop, Corvane (Demo) Aero 14"}


def answer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entities": [
            {
                "class_name": "HardwareAsset",
                "properties": [
                    {"name": "asset_tag", "value": "HW-25-0101"},
                    {"name": "hardware_type", "value": "laptop"},
                ],
                "evidence": [PAGE_EVIDENCE, {"locator": "page 99", "quote": "made up"}],
                "confidence": 0.9,
            }
        ],
        "relations": [
            {
                "relation": "ASSIGNED_TO",
                "source_class": "HardwareAsset",
                "source_key": "HW-25-0101",
                "target_class": "Department",
                "target_key": "FIN",
                "evidence": [{"locator": "page 3", "quote": "is assigned to Finance (FIN)"}],
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
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf")
    cands = extractor(fake).extract(chunks)
    ent = next(c for c in cands if isinstance(c, CandidateEntity))
    rel = next(c for c in cands if isinstance(c, CandidateRelation))
    assert ent.properties == {"asset_tag": "HW-25-0101", "hardware_type": "laptop"}
    assert [(e.source_type, e.source_ref, e.locator) for e in ent.evidence] == [
        ("pdf", "invoice.pdf", "page 1")  # page 99 does not exist: dropped
    ]
    assert (rel.source.key, rel.target.key, rel.confidence) == ("HW-25-0101", "FIN", 0.8)


def test_prompt_contains_ontology_and_page_markers() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer()]})
    extractor(fake).extract(pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf"))
    (req,) = fake.calls  # all three pages fit one window
    assert "[page 1]" in req.user and "[page 2]" in req.user and "[page 3]" in req.user
    assert "HardwareAsset (key: asset_tag" in req.user
    assert "(HardwareAsset)-[:ASSIGNED_TO]->(Department)" in req.user
    assert "invoice.pdf" in req.user


def test_rendered_ontology_shows_datatypes_and_allowed_values() -> None:
    """Review round 1: the LLM must know a property's type and allowed values, or it copies
    '2,150.00' / '3 November 2025' / 'Network switch' verbatim and the schema gate rejects it."""
    text = render_ontology(OntologySpec(CORE_CLASSES, CORE_RELATIONS))
    hw = next(line for line in text.splitlines() if line.startswith("- HardwareAsset "))
    assert "asset_tag*: string" in hw
    assert "cost: float" in hw and "purchase_date: date" in hw
    assert "hardware_type: string (one of: laptop, desktop, server, network)" in hw
    sl = next(line for line in text.splitlines() if line.startswith("- SoftwareLicense "))
    assert "seats: int" in sl and "expiry_date: date" in sl
    assert "total_amount: float" in text and "invoice_date: date" in text


def test_extraction_prompt_asks_for_normalised_values() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer()]})
    extractor(fake).extract(pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf"))
    (req,) = fake.calls
    assert "cost: float" in req.user and "invoice_date: date" in req.user
    system = req.system.lower()
    assert "yyyy-mm-dd" in system
    assert "thousands separators" in system and "currency" in system
    assert "one of" in system  # enumerated values must be used as listed


def test_samples_are_distinct_calls() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer(), answer(relations=[])]})
    ex = extractor(fake)
    chunks = pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf")
    first, second = ex.extract(chunks, sample=0), ex.extract(chunks, sample=1)
    assert len(fake.calls) == 2 and len(first) == 2 and len(second) == 1


def test_off_vocabulary_answer_is_rejected_by_schema() -> None:
    bad = answer(
        entities=[{"class_name": "Company", "properties": [], "evidence": [], "confidence": 1.0}]
    )
    with pytest.raises(LLMOutputError):
        extractor(FakeLLM({EXTRACTION_PROMPT.name: [bad]})).extract(
            pdf_chunks(ROOT / PDF_PATH, source_ref="invoice.pdf")
        )


def test_each_window_is_a_separate_call() -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: [answer(relations=[])]})
    chunks = [SourceChunk("pdf", "a.pdf", f"page {i}", "text " * 300) for i in range(1, 4)]
    ex = DocumentExtractor(
        StructuredLLM(fake, InMemoryCache()), SPEC, ontology_version=1, max_chars=2000
    )
    ex.extract(chunks)
    assert len(fake.calls) == 3


def test_evidence_matches_sheet_names_with_irregular_spacing(tmp_path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Two  Spaces"
    ws.append(["asset_tag", "status"])
    ws.append(["HW-23-0003", "retired"])
    path = tmp_path / "s.xlsx"
    wb.save(path)
    chunks = xlsx_chunks(path, source_ref="s.xlsx")
    assert chunks[0].locator == "sheet Two  Spaces row 2"
    ent = {
        "class_name": "HardwareAsset",
        "properties": [
            {"name": "asset_tag", "value": "HW-23-0003"},
            {"name": "status", "value": "retired"},
        ],
        "evidence": [{"locator": "sheet Two  Spaces row 2", "quote": "retired"}],
        "confidence": 1.0,
    }
    fake = FakeLLM({EXTRACTION_PROMPT.name: [{"entities": [ent], "relations": []}]})
    (cand,) = extractor(fake).extract(chunks)
    assert [e.locator for e in cand.evidence] == ["sheet Two  Spaces row 2"]


@pytest.mark.parametrize(
    "error",
    [
        NotImplementedError("Unsupported filter /Foo"),
        __import__("pypdf.errors", fromlist=["LimitReachedError"]).LimitReachedError("too big"),
        __import__("pypdf.errors", fromlist=["PdfReadError"]).PdfReadError("bad xref"),
    ],
)
def test_any_pdf_parser_failure_becomes_value_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    import writer_service.ingestion.documents as docs

    class Exploding:
        def __init__(self, *_: object) -> None:
            raise error

    monkeypatch.setattr(docs, "PdfReader", Exploding)
    with pytest.raises(ValueError, match="not a readable PDF"):
        pdf_chunks(b"%PDF-1.7 ...", source_ref="x.pdf")


def test_randomly_corrupted_pdfs_never_escape_as_other_errors() -> None:
    import contextlib
    import random

    data = bytearray((ROOT / PDF_PATH).read_bytes())
    rng = random.Random(1)
    for _ in range(200):
        broken = bytearray(data)
        for _ in range(20):
            broken[rng.randrange(len(broken))] = rng.randrange(256)
        with contextlib.suppress(ValueError):  # anything else fails the test
            pdf_chunks(bytes(broken), source_ref="fuzz.pdf")


def test_truncated_sheet_xml_becomes_value_error(tmp_path: Path) -> None:
    """Read-only workbooks parse sheet XML lazily; corruption there must still be a ValueError."""
    import zipfile

    src = zipfile.ZipFile(ROOT / XLSX_PATH)
    broken = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = data[: len(data) // 2]  # cut the sheet XML mid-element
            dst.writestr(info, data)
    src.close()
    with pytest.raises(ValueError, match="not a readable Excel workbook"):
        xlsx_chunks(broken, source_ref="broken.xlsx")
