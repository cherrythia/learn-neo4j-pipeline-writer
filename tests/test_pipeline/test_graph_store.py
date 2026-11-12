"""InMemoryGraph replacement semantics per cardinality (Neo4j version: integration tests).

Seed edges: A→F1, A→F2, B→F1. Then a conflict approval writes a new edge with
replace_existing=True; only the edges the cardinality forbids may disappear."""

import pytest

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec, RelationSpec
from writer_service.pipeline.candidates import CandidateRelation, EntityRef
from writer_service.pipeline.graph_store import InMemoryGraph, audit_view, to_fact

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
    assert {(r["source"]["key"], r["target"]["key"]) for r in replaced} == expected_replaced
    g.undo(replaced)  # compensation puts every removed edge back
    assert {(e[3], e[4]) for e in g.edges} == expected_edges | expected_replaced


def entity(key: str, name: str) -> object:
    from writer_service.pipeline.candidates import CandidateEntity, Evidence

    return CandidateEntity(
        class_name="Issuer",
        properties={"ticker": key, "name": name},
        evidence=[
            Evidence(source_type="pdf", source_ref="a.pdf", locator="page 1", quote=f"{name} quote")
        ],
    )


def test_rollback_of_the_asserting_run_demotes_to_stub() -> None:
    spec = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
    g = InMemoryGraph()
    g.write_run("run1", 1, [to_fact(entity("APH", "Aurora"), spec, 0.9)])  # asserts APH
    filed = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="F1"),
        target=EntityRef(class_name="Issuer", key="APH"),
    )
    g.write_run("run2", 1, [to_fact(filed, spec, 0.9)])  # only references APH
    g.rollback_run("run1")
    aph = g.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["stub"] is True and "name" not in aph
    assert aph["runs"] == ["run2"] and aph["run_id"] == "run2"
    assert not [lk for lk in g.supported if lk[0] == ("Issuer", "aph")]
    assert g.evidence == {}
    assert (
        g.candidates("Issuer", "ticker", "name", "Aurora") == []
    )  # no stale name for fuzzy matching


def test_shared_evidence_keeps_only_the_remaining_runs_links() -> None:
    spec = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
    g = InMemoryGraph()
    g.write_run("run1", 1, [to_fact(entity("APH", "Aurora"), spec, 0.9)])
    g.write_run("run2", 1, [to_fact(entity("APH", "Aurora"), spec, 0.9)])
    g.rollback_run("run1")
    (item,) = g.evidence.values()
    assert item["runs"] == ["run2"] and item["run_id"] == "run2"
    assert all(runs == ["run2"] for runs in g.supported.values())
    assert g.node_properties("Issuer", "ticker", "APH")["stub"] is False  # type: ignore[index]


def test_property_overwrites_are_recorded_and_undone() -> None:
    spec = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
    g = InMemoryGraph()
    g.write_run("run1", 1, [to_fact(entity("APH", "Aurora"), spec, 0.9)])
    changes = g.write_run("run2", 1, [to_fact(entity("APH", "Aurora Renamed"), spec, 0.8)])
    (change,) = [c for c in changes if c["node"]["label"] == "Issuer"]  # one per label
    assert change["node"] == {"label": "Issuer", "key": "APH"}
    assert change["before"] == {"name": "Aurora", "run_id": "run1", "score": 0.9}
    assert "restore" not in audit_view(changes)[0]
    g.undo(changes)
    aph = g.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and (aph["name"], aph["run_id"], aph["score"]) == ("Aurora", "run1", 0.9)
    assert g.candidates("Issuer", "ticker", "name", "Aurora") == [("APH", "Aurora")]


def test_new_entities_record_no_property_changes() -> None:
    spec = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
    g = InMemoryGraph()
    assert g.write_run("run1", 1, [to_fact(entity("APH", "Aurora"), spec, 0.9)]) == []
