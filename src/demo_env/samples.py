"""Build the sample documents used by the document-ingestion path.

Regenerate:  ``uv run python -m demo_env.samples``
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from fpdf import FPDF
from openpyxl import Workbook

from demo_env.data import (
    CURRENCY,
    CUSTOMER_NAME,
    GST_RATE,
    HARDWARE,
    INVOICE_DATE,
    INVOICE_NUMBER,
    INVOICED_HARDWARE,
    INVOICED_LICENSES,
    VENDOR_COUNTRY,
    VENDOR_NAME,
    department_by_code,
    invoice_subtotal,
    invoice_total,
    software_by_id,
)

PDF_PATH = Path("samples/meridian_invoice_INV-2025-0418.pdf")
XLSX_PATH = Path("samples/asset_register_2025.xlsx")
# Fixed timestamp so regenerated files are byte-identical (no spurious git diffs).
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_KIND = {"laptop": "Laptop", "desktop": "Desktop", "server": "Server", "network": "Network switch"}


def long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def money(amount: float) -> str:
    return f"{amount:,.2f}"


def _dept(code: str) -> str:
    return f"{department_by_code(code).name} ({code})"


def hardware_line(i: int) -> str:
    a = INVOICED_HARDWARE[i]
    return (
        f"{a.asset_tag} {_KIND[a.hardware_type]}, {a.manufacturer} {a.model}, "
        f"S/N {a.serial_number}, 1 x {money(a.cost)}"
    )


def license_line(i: int) -> str:
    x = INVOICED_LICENSES[i]
    sw = software_by_id(x.software_id)
    return (
        f"{x.asset_tag} {sw.name} ({x.software_id}), {x.license_type} licence, "
        f"{x.seats} seats x {money(x.unit_price)} = {money(x.amount)}"
    )


def assignment_line(i: int) -> str:
    a = INVOICED_HARDWARE[i]
    return f"{_KIND[a.hardware_type]} {a.asset_tag} is assigned to {_dept(a.dept_code)}."


def allocation_line(i: int) -> str:
    x = INVOICED_LICENSES[i]
    return (
        f"Licence {x.asset_tag} is allocated to {_dept(x.dept_code)} and expires on "
        f"{long_date(x.expiry_date)}."
    )


def _pages() -> list[tuple[str, list[str]]]:
    hw = [hardware_line(i) for i in range(len(INVOICED_HARDWARE))]
    sl = [license_line(i) for i in range(len(INVOICED_LICENSES))]
    gst = invoice_total() - invoice_subtotal()
    return [
        (
            f"Tax Invoice {INVOICE_NUMBER}",
            [
                f"{VENDOR_NAME}, 12 Kallang Way #04-18, {VENDOR_COUNTRY} 349221. "
                "This invoice is fictional and for demonstration only.",
                f"Invoice number: {INVOICE_NUMBER}",
                f"Invoice date: {long_date(INVOICE_DATE)}",
                f"Bill to: {CUSTOMER_NAME}, attention IT Services (ITS)",
                f"Currency: {CURRENCY}",
                "Section A - Hardware (each item carries the asset tag shown):",
                *hw,
            ],
        ),
        (
            "Section B - Software licences",
            [
                "Each licence is recorded as an asset under the tag shown:",
                *sl,
                f"Subtotal: {CURRENCY} {money(invoice_subtotal())}",
                f"GST {GST_RATE:.0%}: {CURRENCY} {money(gst)}",
                f"Total amount due: {CURRENCY} {money(invoice_total())}",
                "Payment terms: 30 days from the invoice date.",
            ],
        ),
        (
            "Delivery and asset tagging notes",
            [
                f"All items on invoice {INVOICE_NUMBER} were delivered and tagged on "
                f"{long_date(INVOICE_DATE)}.",
                *[assignment_line(i) for i in range(len(INVOICED_HARDWARE))],
                *[allocation_line(i) for i in range(len(INVOICED_LICENSES))],
                "Remit to Meridian Systems Supply (Demo). Questions: accounts@meridian.example.",
            ],
        ),
    ]


def build_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.set_creation_date(_FIXED_TS)
    pdf.set_font("Helvetica", size=11)
    for title, paragraphs in _pages():
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=14)
        pdf.multi_cell(0, 8, title)
        pdf.ln(4)
        pdf.set_font("Helvetica", size=10)
        for para in paragraphs:
            pdf.multi_cell(0, 6, para)
            pdf.ln(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


REGISTER_HEADER = (
    "asset_tag",
    "hardware_type",
    "manufacturer",
    "model",
    "serial_number",
    "department",
    "status",
)


def build_xlsx(path: Path) -> None:
    """The asset register: one row per hardware asset (existing fleet and the new invoice)."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Register"
    ws.append(list(REGISTER_HEADER))
    for a in HARDWARE:
        ws.append(
            [
                a.asset_tag,
                a.hardware_type,
                a.manufacturer,
                a.model,
                a.serial_number,
                department_by_code(a.dept_code).name,
                a.status,
            ]
        )
    wb.properties.created = _FIXED_TS.replace(tzinfo=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    _pin_zip_timestamps(path)


def _pin_zip_timestamps(path: Path) -> None:
    """openpyxl stamps the zip entries and ``dcterms:modified`` with the current time at save,
    ignoring ``wb.properties.modified``; rewrite both with a fixed timestamp."""
    with zipfile.ZipFile(path) as src:
        entries = [(info.filename, src.read(info)) for info in src.infolist()]
    fixed = _FIXED_TS.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    entries = [
        (
            name,
            re.sub(rb"(<dcterms:modified[^>]*>)[^<]*(<)", rb"\g<1>" + fixed + rb"\g<2>", data)
            if name == "docProps/core.xml"
            else data,
        )
        for name, data in entries
    ]
    stamp = _FIXED_TS.timetuple()[:6]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)


def main() -> None:
    build_pdf(PDF_PATH)
    build_xlsx(XLSX_PATH)
    print(f"wrote {PDF_PATH} and {XLSX_PATH}")


if __name__ == "__main__":
    main()
