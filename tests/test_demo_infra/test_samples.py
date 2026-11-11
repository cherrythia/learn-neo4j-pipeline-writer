from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader

from demo_env.samples import PDF_PATH, XLSX_PATH, build_pdf, build_xlsx

ROOT = Path(__file__).resolve().parents[2]


def test_sample_pdf_has_pages_and_known_facts() -> None:
    reader = PdfReader(ROOT / PDF_PATH)
    assert len(reader.pages) >= 2
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "Aurora Pacific Holdings (Demo)" in text
    assert "ESG" in text


def test_sample_xlsx_has_checklist_rows() -> None:
    wb = load_workbook(ROOT / XLSX_PATH, read_only=True)
    ws = wb["Checklist"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:4] == ("issuer", "rule_code", "requirement", "status")
    assert len(rows) > 5


def test_builders_are_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    build_xlsx(a)
    build_xlsx(b)
    rows_a = list(load_workbook(a).active.iter_rows(values_only=True))  # type: ignore[union-attr]
    rows_b = list(load_workbook(b).active.iter_rows(values_only=True))  # type: ignore[union-attr]
    assert rows_a == rows_b
    p = tmp_path / "s.pdf"
    build_pdf(p)
    assert PdfReader(p).pages[0].extract_text()
