"""``writer-eval``: measure extraction accuracy against the golden set.

uv run writer-eval                         # replay recordings (offline, CI)
uv run writer-eval --mode live             # call the real LLM (fresh, empty recordings dir)
uv run writer-eval --runs 3 --min-f1 0.9 --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from writer_service.config import get_settings
from writer_service.llm.cache import FileCache
from writer_service.llm.factory import build_llm_client
from writer_service.llm.fake import ReplayOnlyLLM
from writer_service.llm.structured import StructuredLLM

from .runner import Report, evaluate
from .scripted import LIVE_RECORDINGS, RECORDINGS

GOLDEN_DIR = Path("evaluation/golden")


def render(report: Report) -> str:
    lines = [
        f"{report.golden}  (runs: {report.runs}, run-to-run agreement: {report.agreement:.2f})",
        f"  {'':10} {'precision':>9} {'recall':>7} {'f1':>6} {'attr':>6}  tp fp fn",
    ]

    def attr(v: float | None) -> str:
        return f"{v:6.2f}" if v is not None else f"{'n/a':>6}"

    rows = [(f"run {i}", s) for i, s in enumerate(report.per_run)] + [("voted", report.voted)]
    for name, s in rows:
        lines.append(
            f"  {name:10} {s.precision:9.2f} {s.recall:7.2f} {s.f1:6.2f} "
            f"{attr(s.attribute_accuracy)}  {s.true_positives:2} "
            f"{s.false_positives:2} {s.false_negatives:2}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="writer-eval")
    parser.add_argument(
        "--golden",
        type=Path,
        action="append",
        help="golden file(s); default: every file in evaluation/golden",
    )
    parser.add_argument("--runs", type=int, default=None, help="default: self-consistency N")
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument(
        "--recordings",
        type=Path,
        default=None,
        help=f"replay: default {RECORDINGS}; live: default {LIVE_RECORDINGS} (must be empty)",
    )
    parser.add_argument(
        "--min-f1",
        type=float,
        default=0.0,
        help="exit 1 if the voted F1 of any golden file is below this",
    )
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    args = parser.parse_args(argv)

    recordings = args.recordings or (RECORDINGS if args.mode == "replay" else LIVE_RECORDINGS)
    if args.mode == "live" and recordings.is_dir() and any(recordings.iterdir()):
        # StructuredLLM reads its cache first: a non-empty folder would replay old answers and
        # report them as a live measurement.
        parser.error(f"--mode live needs an empty --recordings folder; {recordings} has entries")
    settings = get_settings()
    client = (
        ReplayOnlyLLM(settings.llm_model) if args.mode == "replay" else build_llm_client(settings)
    )
    llm = StructuredLLM(client, FileCache(recordings), max_attempts=settings.llm_max_attempts)
    runs = args.runs or settings.self_consistency_runs
    goldens = args.golden or sorted(GOLDEN_DIR.glob("*.json"))
    reports = [evaluate(llm, g, runs, Path(".")) for g in goldens]
    for r in reports:
        print(render(r))
    if args.json:
        args.json.write_text(json.dumps([r.as_dict() for r in reports], indent=2) + "\n")
    worst = min((r.voted.f1 for r in reports), default=1.0)
    if worst < args.min_f1:
        print(f"FAIL: voted F1 {worst:.2f} < --min-f1 {args.min_f1:.2f}", file=sys.stderr)
        return 1
    return 0


def run() -> None:
    raise SystemExit(main())
