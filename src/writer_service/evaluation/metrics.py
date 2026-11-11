"""Precision / recall / F1 on fact identity, attribute accuracy, and run-to-run agreement."""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass
from typing import Any

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, match_key

AnyCandidate = CandidateEntity | CandidateRelation
FactKey = tuple[str, ...]


def entity_key(class_name: str, key: str) -> FactKey:
    return ("entity", class_name, match_key(key))


def relation_key(relation: str, source: str, target: str) -> FactKey:
    return ("relation", relation, match_key(source), match_key(target))


def fact_keys(candidates: list[AnyCandidate], spec: OntologySpec) -> set[FactKey]:
    """Identity of each fact: wording of other properties doesn't matter here."""
    keys = {c.name: c.identity_key for c in spec.classes}
    out: set[FactKey] = set()
    for c in candidates:
        if isinstance(c, CandidateEntity):
            key = c.properties.get(keys.get(c.class_name, ""))
            if key is not None:
                out.add(entity_key(c.class_name, str(key)))
        else:
            out.add(relation_key(c.relation, c.source.key, c.target.key))
    return out


def golden_keys(golden: dict[str, Any]) -> set[FactKey]:
    return {entity_key(e["class_name"], e["key"]) for e in golden["entities"]} | {
        relation_key(r["relation"], r["source"], r["target"]) for r in golden["relations"]
    }


@dataclass(frozen=True)
class Scores:
    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    attribute_accuracy: float  # share of golden properties reproduced on matched entities

    def as_dict(self) -> dict[str, Any]:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


def score_facts(
    candidates: list[AnyCandidate], golden: dict[str, Any], spec: OntologySpec
) -> Scores:
    predicted, expected = fact_keys(candidates, spec), golden_keys(golden)
    tp, fp, fn = len(predicted & expected), len(predicted - expected), len(expected - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    keys = {c.name: c.identity_key for c in spec.classes}
    by_identity = {}
    for c in candidates:
        if isinstance(c, CandidateEntity):
            k = c.properties.get(keys.get(c.class_name, ""))
            if k is not None:
                by_identity[entity_key(c.class_name, str(k))] = c
    checked = correct = 0
    for e in golden["entities"]:
        cand = by_identity.get(entity_key(e["class_name"], e["key"]))
        if cand is None:
            continue
        for prop, value in e["properties"].items():
            checked += 1
            got = cand.properties.get(prop)
            correct += got is not None and match_key(got) == match_key(value)
    accuracy = correct / checked if checked else 1.0
    return Scores(precision, recall, f1, tp, fp, fn, accuracy)


def agreement(runs: list[set[FactKey]]) -> float:
    """Mean pairwise Jaccard similarity of the facts each run produced (1.0 = identical runs)."""
    if len(runs) < 2:
        return 1.0
    sims = [len(a & b) / len(a | b) if a | b else 1.0 for a, b in itertools.combinations(runs, 2)]
    return sum(sims) / len(sims)
