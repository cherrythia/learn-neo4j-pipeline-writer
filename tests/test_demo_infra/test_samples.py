from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader

from demo_env.data import FILINGS, ISSUERS, OBLIGATIONS, filing_by_id
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


def test_committed_samples_match_fresh_build(tmp_path: Path) -> None:
    """Samples are byte-reproducible, so regenerating never creates a spurious diff."""
    pdf, xlsx = tmp_path / "s.pdf", tmp_path / "s.xlsx"
    build_pdf(pdf)
    build_xlsx(xlsx)
    assert pdf.read_bytes() == (ROOT / PDF_PATH).read_bytes()
    assert xlsx.read_bytes() == (ROOT / XLSX_PATH).read_bytes()


def test_checklist_rows_agree_with_obligations_and_filings() -> None:
    ws = load_workbook(ROOT / XLSX_PATH, read_only=True)["Checklist"]
    rows = list(ws.iter_rows(values_only=True))[1:]
    by_name = {i.name: i for i in ISSUERS}
    for issuer_name, rule_code, _req, status in rows:
        issuer = by_name[str(issuer_name)]
        # the rule must be an obligation for the issuer's board
        assert any(o.rule_code == rule_code and o.board == issuer.board for o in OBLIGATIONS)
        filing = next(
            f
            for f in FILINGS
            if f.issuer_id == issuer.issuer_id
            and str(rule_code).endswith("705" if f.filing_type == "annual_report" else "711A")
        )
        expected = "late" if filing.filed_on > filing.due_on else "filed on time"
        assert status == expected, (issuer_name, rule_code)
    assert any(r[3] == "late" for r in rows)


def test_pdf_dates_match_demo_filings() -> None:
    text = "\n".join(p.extract_text() for p in PdfReader(ROOT / PDF_PATH).pages)
    for fid in ("AR-2025-APH", "ESG-2025-APH"):
        d = filing_by_id(fid).filed_on
        assert f"{d.day} {d.strftime('%B %Y')}" in text, fid
