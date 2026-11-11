"""Fictional compliance demo data: the single source for seed SQL, mock Superset and samples."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Literal

Board = Literal["mainboard", "catalist"]
FilingType = Literal["annual_report", "esg_report"]


@dataclass(frozen=True)
class Issuer:
    issuer_id: int
    name: str
    ticker: str
    sector: str
    board: Board
    country: str


@dataclass(frozen=True)
class Rule:
    rule_code: str
    title: str
    text: str
    filing_type: FilingType


@dataclass(frozen=True)
class Obligation:
    obligation_id: int
    rule_code: str
    board: Board
    deadline_days: int


@dataclass(frozen=True)
class Filing:
    filing_id: str
    issuer_id: int
    filing_type: FilingType
    fiscal_year: int
    due_on: date
    filed_on: date


_ISSUER_SPECS: list[tuple[str, str, str, Board, str]] = [
    ("Aurora Pacific Holdings", "APH", "Energy", "mainboard", "SG"),
    ("Banyan Logistics", "BYL", "Industrials", "mainboard", "SG"),
    ("Cobalt Ridge Mining", "CRM", "Materials", "catalist", "AU"),
    ("Delta Harbour REIT", "DHR", "Real Estate", "mainboard", "SG"),
    ("Evergreen Agritech", "EGA", "Consumer Staples", "catalist", "MY"),
    ("Falcon Microsystems", "FMS", "Technology", "mainboard", "SG"),
    ("Granite Bay Bank", "GBB", "Financials", "mainboard", "SG"),
    ("Horizon Health Group", "HHG", "Healthcare", "mainboard", "SG"),
    ("Iris Water Solutions", "IWS", "Utilities", "catalist", "ID"),
    ("Jade Lotus Foods", "JLF", "Consumer Staples", "catalist", "SG"),
    ("Kestrel Aviation Services", "KAS", "Industrials", "mainboard", "SG"),
    ("Lumen Solar", "LMS", "Energy", "catalist", "VN"),
    ("Monsoon Textiles", "MNT", "Consumer Discretionary", "catalist", "TH"),
    ("Nimbus Cloud Networks", "NCN", "Technology", "mainboard", "SG"),
    ("Orchid Insurance", "ORI", "Financials", "mainboard", "SG"),
    ("Pearl Shipping Lines", "PSL", "Industrials", "mainboard", "HK"),
    ("Quartz Semiconductor", "QZS", "Technology", "mainboard", "SG"),
    ("Rainforest Timber", "RFT", "Materials", "catalist", "MY"),
    ("Saffron Hospitality", "SFH", "Consumer Discretionary", "catalist", "SG"),
    ("Tidewater Ports", "TWP", "Industrials", "mainboard", "SG"),
]

ISSUERS: list[Issuer] = [
    Issuer(i, f"{name} (Demo)", ticker, sector, board, country)
    for i, (name, ticker, sector, board, country) in enumerate(_ISSUER_SPECS, start=1)
]

RULES: list[Rule] = [
    Rule(
        "LR-705",
        "Annual report timing",
        "An issuer must issue its annual report within 4 months of its financial year end.",
        "annual_report",
    ),
    Rule(
        "LR-707",
        "Annual report contents",
        "The annual report must include the auditor's report and the directors' statement.",
        "annual_report",
    ),
    Rule(
        "LR-711A",
        "Sustainability report",
        "An issuer must issue a sustainability report for each financial year.",
        "esg_report",
    ),
    Rule(
        "LR-711B",
        "Climate disclosures",
        "The sustainability report must include climate-related disclosures.",
        "esg_report",
    ),
    Rule(
        "CR-705",
        "Catalist annual report timing",
        "A Catalist issuer must issue its annual report within 4 months of year end, "
        "with its sponsor's review.",
        "annual_report",
    ),
    Rule(
        "CR-711A",
        "Catalist sustainability report",
        "A Catalist issuer must issue a sustainability report within 5 months of year end.",
        "esg_report",
    ),
]

OBLIGATIONS: list[Obligation] = [
    Obligation(1, "LR-705", "mainboard", 120),
    Obligation(2, "LR-707", "mainboard", 120),
    Obligation(3, "LR-711A", "mainboard", 120),
    Obligation(4, "LR-711B", "mainboard", 120),
    Obligation(5, "CR-705", "catalist", 120),
    Obligation(6, "CR-711A", "catalist", 150),
]

FISCAL_YEAR = 2025
_YEAR_END = date(FISCAL_YEAR, 12, 31)
# The timing rule that decides whether a filing is on time, per (board, filing type).
TIMING_RULE: dict[tuple[Board, FilingType], str] = {
    ("mainboard", "annual_report"): "LR-705",
    ("mainboard", "esg_report"): "LR-711A",
    ("catalist", "annual_report"): "CR-705",
    ("catalist", "esg_report"): "CR-711A",
}
# Deadlines are derived from the obligations table, so there is one source for them.
_DEADLINES: dict[tuple[Board, FilingType], int] = {
    key: next(o.deadline_days for o in OBLIGATIONS if o.rule_code == code and o.board == key[0])
    for key, code in TIMING_RULE.items()
}
# Issuer ids whose filing of that type is late (gives the pipeline real findings to discover).
_LATE: set[tuple[int, FilingType]] = {(3, "esg_report"), (9, "annual_report"), (13, "esg_report")}


def _filing(issuer: Issuer, ftype: FilingType) -> Filing:
    due = _YEAR_END + timedelta(days=_DEADLINES[(issuer.board, ftype)])
    offset = 12 if (issuer.issuer_id, ftype) in _LATE else -(issuer.issuer_id % 20) - 1
    prefix = "AR" if ftype == "annual_report" else "ESG"
    return Filing(
        filing_id=f"{prefix}-{FISCAL_YEAR}-{issuer.ticker}",
        issuer_id=issuer.issuer_id,
        filing_type=ftype,
        fiscal_year=FISCAL_YEAR,
        due_on=due,
        filed_on=due + timedelta(days=offset),
    )


FILINGS: list[Filing] = [_filing(i, t) for i in ISSUERS for t in ("annual_report", "esg_report")]

TABLES: dict[str, type] = {
    "issuers": Issuer,
    "filings": Filing,
    "rules": Rule,
    "obligations": Obligation,
}
ROWS: dict[str, list[object]] = {
    "issuers": list(ISSUERS),
    "filings": list(FILINGS),
    "rules": list(RULES),
    "obligations": list(OBLIGATIONS),
}


def timing_rule_for(filing: Filing) -> Rule:
    board = next(i.board for i in ISSUERS if i.issuer_id == filing.issuer_id)
    code = TIMING_RULE[(board, filing.filing_type)]
    return next(r for r in RULES if r.rule_code == code)


def filing_by_id(filing_id: str) -> Filing:
    return next(f for f in FILINGS if f.filing_id == filing_id)


def columns_of(table: str) -> tuple[str, ...]:
    return tuple(f.name for f in fields(TABLES[table]))


# Superset view of the demo tables (used by the mock API and the real-Superset bootstrap).
SCHEMA = "demo_compliance"


@dataclass(frozen=True)
class Dataset:
    id: int
    table_name: str


DATASETS: list[Dataset] = [Dataset(i, t) for i, t in enumerate(TABLES, start=1)]
