"""Scripted stand-in recordings for CI.

There is no API key in CI, so ``evaluation/recordings`` holds *scripted* extractor answers,
written into the same ``FileCache`` format a real ``writer-eval --mode live`` run produces and
keyed under the configured model name. They mimic a realistic, imperfect extractor so the
report is meaningful: run 0 invents a rule, run 1 misses one, run 2 is clean.

Regenerate after changing the prompt, ontology or golden set:
    uv run python -m writer_service.evaluation.scripted
Replace with real recordings:  writer-eval --mode live   (needs WRITER_ANTHROPIC_API_KEY)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from writer_service.ingestion.documents import EXTRACTION_PROMPT
from writer_service.llm.cache import FileCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM

from .runner import evaluate

RECORDINGS = Path("evaluation/recordings")
GOLDEN = Path("evaluation/golden/aurora_annual_report_2025.json")
MODEL = "claude-sonnet-5-5"


def _ent(cls: str, props: dict[str, str], page: int, quote: str) -> dict[str, Any]:
    return {
        "class_name": cls,
        "properties": [{"name": k, "value": v} for k, v in props.items()],
        "evidence": [{"locator": f"page {page}", "quote": quote}],
        "confidence": 0.9,
    }


def _rel(rel: str, sc: str, sk: str, tc: str, tk: str, page: int, quote: str) -> dict[str, Any]:
    return {
        "relation": rel,
        "source_class": sc,
        "source_key": sk,
        "target_class": tc,
        "target_key": tk,
        "evidence": [{"locator": f"page {page}", "quote": quote}],
        "confidence": 0.85,
    }


BASE_ENTITIES = [
    _ent(
        "Issuer",
        {
            "ticker": "APH",
            "name": "Aurora Pacific Holdings (Demo)",
            "board": "mainboard",
            "country": "SG",
        },
        1,
        "Aurora Pacific Holdings (Demo) (ticker APH) is listed on the Mainboard",
    ),
    _ent(
        "Filing",
        {
            "filing_id": "AR-2025-APH",
            "filing_type": "annual_report",
            "fiscal_year": "2025",
            "filed_on": "2026-04-28",
        },
        1,
        "(filing AR-2025-APH)",
    ),
    _ent(
        "Filing",
        {
            "filing_id": "ESG-2025-APH",
            "filing_type": "esg_report",
            "fiscal_year": "2025",
            "filed_on": "2026-04-28",
        },
        2,
        "(filing ESG-2025-APH)",
    ),
    _ent("Rule", {"rule_code": "LR-705"}, 1, "Listing Rule LR-705"),
    _ent("Rule", {"rule_code": "LR-707"}, 1, "Listing Rule LR-707"),
    _ent("Rule", {"rule_code": "LR-711A"}, 2, "Listing Rule LR-711A"),
    _ent("Rule", {"rule_code": "LR-711B"}, 2, "LR-711B"),
]
BASE_RELATIONS = [
    _rel("FILED_BY", "Filing", "AR-2025-APH", "Issuer", "APH", 1, "(filing AR-2025-APH)"),
    _rel("FILED_BY", "Filing", "ESG-2025-APH", "Issuer", "APH", 2, "(filing ESG-2025-APH)"),
    _rel("CITES", "Filing", "AR-2025-APH", "Rule", "LR-705", 1, "Listing Rule LR-705"),
    _rel("CITES", "Filing", "AR-2025-APH", "Rule", "LR-707", 1, "Listing Rule LR-707"),
    _rel("CITES", "Filing", "ESG-2025-APH", "Rule", "LR-711A", 2, "Listing Rule LR-711A"),
    _rel("CITES", "Filing", "ESG-2025-APH", "Rule", "LR-711B", 2, "LR-711B"),
]
HALLUCINATED = _ent("Rule", {"rule_code": "LR-999"}, 1, "Listing Rule LR-999")
MISSING = "LR-707"

RUNS: list[dict[str, Any]] = [
    {"entities": [*BASE_ENTITIES, HALLUCINATED], "relations": BASE_RELATIONS},  # invents one
    {
        "entities": [e for e in BASE_ENTITIES if MISSING not in str(e)],  # misses one
        "relations": [r for r in BASE_RELATIONS if r["target_key"] != MISSING],
    },
    {"entities": BASE_ENTITIES, "relations": BASE_RELATIONS},  # clean
]


def record(directory: Path = RECORDINGS, root: Path = Path(".")) -> None:
    """Write the scripted answers as recordings (one per self-consistency sample)."""
    fake = FakeLLM({EXTRACTION_PROMPT.name: RUNS}, model=MODEL)
    evaluate(StructuredLLM(fake, FileCache(directory)), root / GOLDEN, len(RUNS), root)


if __name__ == "__main__":
    record()
    print(f"wrote scripted recordings to {RECORDINGS}")
