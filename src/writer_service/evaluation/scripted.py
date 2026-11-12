"""Scripted stand-in recordings for CI.

There is no API key in CI, so ``evaluation/recordings`` holds *scripted* extractor answers,
written into the same ``FileCache`` format a real ``writer-eval --mode live`` run produces and
keyed under the configured model name. They mimic a realistic, imperfect extractor so the
report is meaningful: run 0 invents an asset, run 1 misses one, run 2 is clean.

Regenerate after changing the prompt, ontology or golden set:
    uv run python -m writer_service.evaluation.scripted
Real answers: ``writer-eval --mode live`` (needs WRITER_ANTHROPIC_API_KEY) writes them to
``evaluation/recordings-live``; CI replays that folder too when it is committed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from writer_service.config import Settings
from writer_service.ingestion.documents import EXTRACTION_PROMPT
from writer_service.llm.cache import FileCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM

from .runner import evaluate

RECORDINGS = Path("evaluation/recordings")
GOLDEN = Path("evaluation/golden/meridian_invoice_2025_0418.json")
LIVE_RECORDINGS = Path("evaluation/recordings-live")  # real answers from --mode live
# Keyed under the default model, so a plain `writer-eval` replays them. (With WRITER_LLM_MODEL
# set to something else, replay misses and fails loudly instead of scoring stale answers.)
MODEL = str(Settings.model_fields["llm_model"].default)

Quote = tuple[int, str]  # (page, verbatim quote)


def _evidence(quotes: tuple[Quote, ...]) -> list[dict[str, str]]:
    return [{"locator": f"page {page}", "quote": quote} for page, quote in quotes]


def _ent(cls: str, props: dict[str, str], *quotes: Quote) -> dict[str, Any]:
    return {
        "class_name": cls,
        "properties": [{"name": k, "value": v} for k, v in props.items()],
        "evidence": _evidence(quotes),
        "confidence": 0.9,
    }


def _rel(rel: str, sc: str, sk: str, tc: str, tk: str, *quotes: Quote) -> dict[str, Any]:
    return {
        "relation": rel,
        "source_class": sc,
        "source_key": sk,
        "target_class": tc,
        "target_key": tk,
        "evidence": _evidence(quotes),
        "confidence": 0.85,
    }


INVOICE = "INV-2025-0418"
VENDOR = "Meridian Systems Supply (Demo)"
_DELIVERED = (3, f"All items on invoice {INVOICE} were delivered and tagged")
# (asset tag, type, manufacturer, model, serial, cost, department, invoice line, assignment line)
_HARDWARE = [
    ("HW-25-0101", "laptop", "Corvane (Demo)", "Aero 14", "CVA14-8F2K91", "2150.00", "FIN",
     "HW-25-0101 Laptop, Corvane (Demo) Aero 14, S/N CVA14-8F2K91",
     "Laptop HW-25-0101 is assigned to Finance (FIN)"),
    ("HW-25-0102", "laptop", "Corvane (Demo)", "Aero 14", "CVA14-8F2K94", "2150.00", "ENG",
     "HW-25-0102 Laptop, Corvane (Demo) Aero 14, S/N CVA14-8F2K94",
     "Laptop HW-25-0102 is assigned to Engineering (ENG)"),
    ("HW-25-0103", "laptop", "Corvane (Demo)", "Aero 16", "CVA16-3M7Q05", "2890.00", "ENG",
     "HW-25-0103 Laptop, Corvane (Demo) Aero 16, S/N CVA16-3M7Q05",
     "Laptop HW-25-0103 is assigned to Engineering (ENG)"),
    ("HW-25-0110", "server", "Halden Compute (Demo)", "R640X", "HDR-77120-A", "14800.00", "ITS",
     "HW-25-0110 Server, Halden Compute (Demo) R640X, S/N HDR-77120-A",
     "Server HW-25-0110 is assigned to IT Services (ITS)"),
    ("HW-25-0120", "network", "Tessel Networks (Demo)", "SW-48", "TSW48-00931", "3420.00", "ITS",
     "HW-25-0120 Network switch, Tessel Networks (Demo) SW-48, S/N TSW48-00931",
     "Network switch HW-25-0120 is assigned to IT Services (ITS)"),
]  # fmt: skip
# (asset tag, software id, software name, seats, department, invoice line, allocation line)
_LICENSES = [
    ("SL-25-0201", "SW-QDOC", "Quillstack Docs", "50", "ENG",
     "SL-25-0201 Quillstack Docs (SW-QDOC), subscription licence, 50 seats",
     "Licence SL-25-0201 is allocated to Engineering (ENG) and expires on 31 October 2026"),
    ("SL-25-0202", "SW-LEDG", "Ledgerline Accounting", "10", "FIN",
     "SL-25-0202 Ledgerline Accounting (SW-LEDG), subscription licence, 10 seats",
     "Licence SL-25-0202 is allocated to Finance (FIN) and expires on 31 October 2026"),
]  # fmt: skip
_DEPARTMENTS = [("FIN", "Finance"), ("ENG", "Engineering"), ("ITS", "IT Services")]

BASE_ENTITIES = [
    _ent(
        "Vendor",
        {"name": VENDOR, "country": "Singapore"},
        (1, f"{VENDOR}, 12 Kallang Way #04-18, Singapore"),
    ),
    _ent(
        "Invoice",
        {
            "invoice_number": INVOICE,
            "invoice_date": "2025-11-03",
            "total_amount": "44482.90",
            "currency": "SGD",
        },
        (1, f"Invoice number: {INVOICE}"),
        (1, "Invoice date: 3 November 2025"),
        (2, "Total amount due: SGD 44,482.90"),
    ),
    *[_ent("Department", {"dept_code": c, "name": n}, (3, f"{n} ({c})")) for c, n in _DEPARTMENTS],
    *[
        _ent(
            "HardwareAsset",
            {
                "asset_tag": tag,
                "hardware_type": kind,
                "manufacturer": maker,
                "model": model,
                "serial_number": serial,
                "cost": cost,
            },
            (1, line),
        )
        for tag, kind, maker, model, serial, cost, _d, line, _a in _HARDWARE
    ],
    *[
        _ent(
            "SoftwareLicense",
            {
                "asset_tag": tag,
                "license_type": "subscription",
                "seats": seats,
                "expiry_date": "2026-10-31",
            },
            (2, line),
            (3, alloc),
        )
        for tag, _sw, _n, seats, _d, line, alloc in _LICENSES
    ],
    *[
        _ent("Software", {"software_id": sw, "name": name}, (2, f"{name} ({sw})"))
        for _t, sw, name, _s, _d, _l, _a in _LICENSES
    ],
]
BASE_RELATIONS = [
    _rel("ISSUED_BY", "Invoice", INVOICE, "Vendor", VENDOR, (1, f"Tax Invoice {INVOICE} {VENDOR}")),
    *[
        _rel("BILLED_ON", "HardwareAsset", tag, "Invoice", INVOICE, _DELIVERED)
        for tag, *_ in _HARDWARE
    ],
    *[
        _rel("BILLED_ON", "SoftwareLicense", tag, "Invoice", INVOICE, _DELIVERED)
        for tag, *_ in _LICENSES
    ],
    *[
        _rel("ASSIGNED_TO", "HardwareAsset", tag, "Department", dept, (3, assigned))
        for tag, _k, _m, _mo, _s, _c, dept, _l, assigned in _HARDWARE
    ],
    *[
        _rel("LICENSES", "SoftwareLicense", tag, "Software", sw, (2, line))
        for tag, sw, _n, _s, _d, line, _a in _LICENSES
    ],
    *[
        _rel("ALLOCATED_TO", "SoftwareLicense", tag, "Department", dept, (3, alloc))
        for tag, _sw, _n, _s, dept, _l, alloc in _LICENSES
    ],
]
# Run 0 invents a fifth laptop; run 1 misses HW-25-0103 (the entity and both of its relations).
HALLUCINATED = _ent(
    "HardwareAsset",
    {"asset_tag": "HW-25-0104", "hardware_type": "laptop"},
    (1, "HW-25-0104 Laptop, Corvane (Demo) Aero 14"),
)
MISSING = "HW-25-0103"

RUNS: list[dict[str, Any]] = [
    {"entities": [*BASE_ENTITIES, HALLUCINATED], "relations": BASE_RELATIONS},  # invents one
    {
        "entities": [e for e in BASE_ENTITIES if MISSING not in str(e)],  # misses one
        "relations": [r for r in BASE_RELATIONS if r["source_key"] != MISSING],
    },
    {"entities": BASE_ENTITIES, "relations": BASE_RELATIONS},  # clean
]


def record(directory: Path = RECORDINGS, root: Path = Path(".")) -> None:
    """Write the scripted answers as recordings (one per self-consistency sample).
    Old entries are removed first: otherwise the cache would serve them back unchanged."""
    for stale in directory.glob("*.json"):
        stale.unlink()
    fake = FakeLLM({EXTRACTION_PROMPT.name: RUNS}, model=MODEL)
    evaluate(StructuredLLM(fake, FileCache(directory)), root / GOLDEN, len(RUNS), root)


if __name__ == "__main__":
    record()
    print(f"wrote scripted recordings to {RECORDINGS}")
