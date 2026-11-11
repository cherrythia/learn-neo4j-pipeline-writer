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

from demo_env.data import FILINGS, ISSUERS, filing_by_id, timing_rule_for

PDF_PATH = Path("samples/aurora_pacific_annual_report_2025.pdf")
XLSX_PATH = Path("samples/disclosure_checklist_2025.xlsx")
# Fixed timestamp so regenerated files are byte-identical (no spurious git diffs).
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)

_APH_ANNUAL = filing_by_id("AR-2025-APH")
_APH_ESG = filing_by_id("ESG-2025-APH")


def _long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


_PAGES = [
    (
        "Annual Report 2025 - Aurora Pacific Holdings (Demo)",
        [
            "Aurora Pacific Holdings (Demo) (ticker APH) is listed on the Mainboard and is "
            "incorporated in Singapore. This document is fictional and for demonstration only.",
            "Aurora Pacific Holdings (Demo) issued this annual report for the financial year "
            f"ended 31 December 2025 on {_long_date(_APH_ANNUAL.filed_on)}, in accordance with "
            "Listing Rule LR-705.",
            "The annual report includes the independent auditor's report and the directors' "
            "statement as required by Listing Rule LR-707.",
        ],
    ),
    (
        "Sustainability (ESG) Statement",
        [
            "Aurora Pacific Holdings (Demo) published its ESG sustainability report for 2025 on "
            f"{_long_date(_APH_ESG.filed_on)} under Listing Rule LR-711A.",
            "Climate-related disclosures aligned with LR-711B are included, covering Scope 1 and "
            "Scope 2 emissions.",
            "The board identified one finding: Scope 3 emissions data was not yet assured.",
        ],
    ),
]


def build_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.set_creation_date(_FIXED_TS)
    pdf.set_font("Helvetica", size=11)
    for title, paragraphs in _PAGES:
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=14)
        pdf.multi_cell(0, 8, title)
        pdf.ln(4)
        pdf.set_font("Helvetica", size=11)
        for para in paragraphs:
            pdf.multi_cell(0, 6, para)
            pdf.ln(3)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def build_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Checklist"
    ws.append(["issuer", "rule_code", "requirement", "status"])
    names = {i.issuer_id: i.name for i in ISSUERS}
    for f in FILINGS[:12]:
        # Each row checks the filing against the *timing* rule for its issuer's board.
        rule = timing_rule_for(f)
        status = "late" if f.filed_on > f.due_on else "filed on time"
        ws.append([names[f.issuer_id], rule.rule_code, rule.title, status])
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
