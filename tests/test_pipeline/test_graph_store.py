"""InMemoryGraph replacement semantics per cardinality (Neo4j version: integration tests).

Seed edges: A→F1, A→F2, B→F1. Then a conflict approval writes a new edge with
replace_existing=True; only the edges the cardinality forbids may disappear."""

import pytest

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec, RelationSpec
from writer_service.pipeline.candidates import CandidateRelation, EntityRef
from writer_service.pipeline.graph_store import InMemoryGraph, to_fact

SEED = (("A", "F1"), ("A", "F2"), ("B", "F1"))


def link(src: str, tgt: str) -> CandidateRelation:
    return CandidateRelation(
        relation="LINK",
        source=EntityRef(class_name="Issuer", key=src),
        target=EntityRef(class_name="Filing", key=tgt),
    )


@pytest.mark.parametrize(
    "cardinality,new,expected_edges,expected_replaced",
    [
        # one target per source: A's other targets go
        ("many_to_one", ("A", "F9"), {("B", "F1"), ("A", "F9")}, {("A", "F1"), ("A", "F2")}),
        # one source per target: F1's other sources go
        ("one_to_many", ("C", "F1"), {("A", "F2"), ("C", "F1")}, {("A", "F1"), ("B", "F1")}),
        # both
        ("one_to_one", ("A", "F1"), {("A", "F1")}, {("A", "F2"), ("B", "F1")}),
        # nothing is ever replaced
        ("many_to_many", ("A", "F9"), {*SEED, ("A", "F9")}, set()),
    ],
)
def test_replacement_removes_only_what_the_cardinality_forbids(
    cardinality: str,
    new: tuple[str, str],
    expected_edges: set[tuple[str, str]],
    expected_replaced: set[tuple[str, str]],
) -> None:
    spec = OntologySpec(
        CORE_CLASSES, (*CORE_RELATIONS, RelationSpec("LINK", "Issuer", "Filing", cardinality))
    )  # type: ignore[arg-type]
    g = InMemoryGraph()
    g.write_run("seed", 1, [to_fact(link(s, t), spec, 1.0) for s, t in SEED])
    replaced = g.write_run("approval", 1, [to_fact(link(*new), spec, 1.0, replace_existing=True)])
    assert {(e[3], e[4]) for e in g.edges} == expected_edges
    assert {(r["source"], r["target"]) for r in replaced} == expected_replaced
