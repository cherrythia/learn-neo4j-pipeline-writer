from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from pypdf import PdfReader

from demo_env import data
from demo_env.samples import (
    PDF_PATH,
    REGISTER_HEADER,
    XLSX_PATH,
    build_pdf,
    build_xlsx,
    long_date,
    money,
)

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def register_rows(path: Path) -> Iterator[list[tuple[Any, ...]]]:
    """Read the Register sheet and always close the file (read-only workbooks keep it open)."""
    wb = load_workbook(path, read_only=True)
    try:
        yield list(wb["Register"].iter_rows(values_only=True))
    finally:
        wb.close()


def pdf_pages() -> list[str]:
    return [p.extract_text() for p in PdfReader(ROOT / PDF_PATH).pages]


def test_sample_pdf_is_a_multi_page_vendor_invoice() -> None:
    pages = pdf_pages()
    assert len(pages) == 3
    text = "\n".join(pages)
    assert data.VENDOR_NAME in text
    assert f"Invoice number: {data.INVOICE_NUMBER}" in text
    assert "fictional" in text


def test_sample_xlsx_has_register_rows() -> None:
    with register_rows(ROOT / XLSX_PATH) as rows:
        pass
    assert rows[0] == REGISTER_HEADER
    assert len(rows) == len(data.HARDWARE) + 1


def test_builders_are_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    build_xlsx(a)
    build_xlsx(b)
    with register_rows(a) as rows_a, register_rows(b) as rows_b:
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


def test_register_rows_agree_with_the_cmdb() -> None:
    with register_rows(ROOT / XLSX_PATH) as all_rows:
        rows = all_rows[1:]
    by_tag = {a.asset_tag: a for a in data.HARDWARE}
    for tag, kind, maker, model, serial, dept, status in rows:
        asset = by_tag[str(tag)]
        assert (kind, maker, model, serial, status) == (
            asset.hardware_type,
            asset.manufacturer,
            asset.model,
            asset.serial_number,
            asset.status,
        )
        assert dept == data.department_by_code(asset.dept_code).name
    assert any(r[6] == "retired" for r in rows)


def test_pdf_lists_every_invoiced_item_with_tag_serial_and_department() -> None:
    page1, page2, page3 = pdf_pages()
    for a in data.INVOICED_HARDWARE:
        assert a.asset_tag in page1 and a.serial_number in page1 and money(a.cost) in page1
        dept = data.department_by_code(a.dept_code)
        assert f"{a.asset_tag} is assigned to {dept.name} ({dept.dept_code})" in page3
    for x in data.INVOICED_LICENSES:
        sw = data.software_by_id(x.software_id)
        assert f"{x.asset_tag} {sw.name} ({sw.software_id})" in page2
        assert f"{x.seats} seats" in page2
        assert long_date(x.expiry_date) in page3


def test_pdf_totals_match_the_demo_invoice() -> None:
    _, page2, _ = pdf_pages()
    assert f"Subtotal: {data.CURRENCY} {money(data.invoice_subtotal())}" in page2
    assert f"Total amount due: {data.CURRENCY} {money(data.invoice_total())}" in page2
    assert long_date(data.INVOICE_DATE) in pdf_pages()[0]
