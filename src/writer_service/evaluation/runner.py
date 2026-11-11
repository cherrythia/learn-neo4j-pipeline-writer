"""Run the document extractor N times on a golden document and score each run, the
self-consistency vote, and the run-to-run agreement."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from writer_service.ingestion.documents import DocumentExtractor, pdf_chunks, xlsx_chunks
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.verification import self_consistency

from .metrics import Scores, agreement, fact_keys, score_facts

CORE_SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
ONTOLOGY_VERSION = 1  # golden sets are labelled against the core ontology v1


@dataclass
class Report:
    golden: str
    runs: int
    per_run: list[Scores]
    voted: Scores
    agreement: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "golden": self.golden,
            "runs": self.runs,
            "per_run": [s.as_dict() for s in self.per_run],
            "voted": self.voted.as_dict(),
            "agreement": round(self.agreement, 4),
        }


def load_golden(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def chunks_for(golden: dict[str, Any], root: Path) -> list[Any]:
    doc = root / golden["document"]
    parse = pdf_chunks if doc.suffix.lower() == ".pdf" else xlsx_chunks
    return parse(doc, source_ref=golden["source_ref"])


def evaluate(
    llm: StructuredLLM, golden_path: Path, runs: int, root: Path, spec: OntologySpec = CORE_SPEC
) -> Report:
    golden = load_golden(golden_path)
    chunks = chunks_for(golden, root)
    extractor = DocumentExtractor(llm, spec, ONTOLOGY_VERSION)
    per_run_candidates = [extractor.extract(chunks, sample=i) for i in range(runs)]
    voted = self_consistency(lambda i: per_run_candidates[i], runs)
    return Report(
        golden=golden_path.name,
        runs=runs,
        per_run=[score_facts(c, golden, spec) for c in per_run_candidates],
        voted=score_facts([v.candidate for v in voted], golden, spec),
        agreement=agreement([fact_keys(c, spec) for c in per_run_candidates]),
    )
