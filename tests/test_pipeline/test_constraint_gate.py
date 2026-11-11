from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef
from writer_service.pipeline.gates import check_batch
from writer_service.pipeline.graph_view import InMemoryGraphView


def ent(cls: str, **props: object) -> CandidateEntity:
    return CandidateEntity(class_name=cls, properties=props)  # type: ignore[arg-type]


def filed_by(filing: str, issuer: str) -> CandidateRelation:
    return CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key=filing),
        target=EntityRef(class_name="Issuer", key=issuer),
    )


def test_clean_candidates_pass(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    results = check_batch(
        [ent("Issuer", ticker="APH", name="Aurora"), filed_by("AR-2025-APH", "APH")], spec, graph
    )
    assert [r.status for r in results] == ["pass", "pass"]


def test_schema_failure_is_reject_and_skips_constraint_checks(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    (r,) = check_batch([ent("Company", name="x")], spec, graph)
    assert r.status == "reject" and r.conflict is None


def test_property_contradiction_with_graph_is_conflict(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    graph.add_node(
        "Issuer", "ticker", "APH", {"ticker": "APH", "name": "Aurora", "board": "mainboard"}
    )
    (r,) = check_batch([ent("Issuer", ticker="aph", name="aurora", board="catalist")], spec, graph)
    assert r.status == "conflict"
    assert r.conflict == (
        "Issuer 'aph': property 'board' is 'mainboard' in the graph, candidate says 'catalist'"
    )


def test_same_values_with_different_spacing_or_case_do_not_conflict(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    graph.add_node("Issuer", "ticker", "APH", {"ticker": "APH", "name": "Aurora Pacific"})
    (r,) = check_batch([ent("Issuer", ticker="APH", name=" aurora  pacific")], spec, graph)
    assert r.status == "pass"


def test_many_to_one_second_target_in_graph_is_conflict(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "APH"))
    (same,) = check_batch([filed_by("ar-2025-aph", "APH")], spec, graph)
    assert same.status == "pass"  # re-asserting the same fact is fine
    (other,) = check_batch([filed_by("AR-2025-APH", "BYL")], spec, graph)
    assert other.status == "conflict"
    assert "FILED_BY is many_to_one" in other.conflict  # type: ignore[operator]
    assert "already points to Issuer 'APH'" in other.conflict  # type: ignore[operator]


def test_many_to_one_contradiction_within_batch(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    results = check_batch([filed_by("F1", "APH"), filed_by("F1", "BYL")], spec, graph)
    assert [r.status for r in results] == ["pass", "conflict"]
    assert "in this batch" in results[1].conflict  # type: ignore[operator]


def test_property_contradiction_within_batch(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    results = check_batch(
        [
            ent("Issuer", ticker="APH", name="A", board="mainboard"),
            ent("Issuer", ticker="APH", name="A", board="catalist"),
        ],
        spec,
        graph,
    )
    assert [r.status for r in results] == ["pass", "conflict"]


def test_many_to_many_allows_multiple_targets(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    cites = [
        CandidateRelation(
            relation="CITES",
            source=EntityRef(class_name="Filing", key="F1"),
            target=EntityRef(class_name="Rule", key=code),
        )
        for code in ("LR-705", "LR-707")
    ]
    assert [r.status for r in check_batch(cites, spec, graph)] == ["pass", "pass"]


def test_one_to_many_checks_the_source_side(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    from writer_service.ontology.spec import RelationSpec

    owns = OntologySpec(
        spec.classes, (*spec.relations, RelationSpec("ISSUED", "Issuer", "Filing", "one_to_many"))
    )
    graph.add_edge("ISSUED", ("Issuer", "ticker", "APH"), ("Filing", "filing_id", "F1"))
    r = CandidateRelation(
        relation="ISSUED",
        source=EntityRef(class_name="Issuer", key="BYL"),
        target=EntityRef(class_name="Filing", key="F1"),
    )
    (res,) = check_batch([r], owns, graph)
    assert res.status == "conflict" and "already has source Issuer 'APH'" in res.conflict  # type: ignore[operator]


def with_relations(spec: OntologySpec, *rels: object) -> OntologySpec:
    return OntologySpec(spec.classes, (*spec.relations, *rels))  # type: ignore[arg-type]


def test_many_to_one_sees_existing_edge_to_a_subclass_node(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    """Range is Organisation; the graph edge points to an Issuer (subclass) node."""
    from writer_service.ontology.spec import RelationSpec

    audited = with_relations(
        spec, RelationSpec("AUDITED_BY", "Filing", "Organisation", "many_to_one")
    )
    graph.add_edge("AUDITED_BY", ("Filing", "filing_id", "F1"), ("Issuer", "ticker", "APH"))
    kpmg = CandidateRelation(
        relation="AUDITED_BY",
        source=EntityRef(class_name="Filing", key="F1"),
        target=EntityRef(class_name="Organisation", key="KPMG"),
    )
    (res,) = check_batch([kpmg], audited, graph)
    assert res.status == "conflict" and "Issuer 'APH'" in res.conflict  # type: ignore[operator]
    same = kpmg.model_copy(update={"target": EntityRef(class_name="Issuer", key="aph")})
    assert check_batch([same], audited, graph)[0].status == "pass"


def test_one_to_many_contradiction_within_batch(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    from writer_service.ontology.spec import RelationSpec

    issued = with_relations(spec, RelationSpec("ISSUED", "Issuer", "Filing", "one_to_many"))
    rels = [
        CandidateRelation(
            relation="ISSUED",
            source=EntityRef(class_name="Issuer", key=src),
            target=EntityRef(class_name="Filing", key="F1"),
        )
        for src in ("APH", "BYL")
    ]
    results = check_batch(rels, issued, graph)
    assert [r.status for r in results] == ["pass", "conflict"]
    assert "has source Issuer 'APH' in this batch" in results[1].conflict  # type: ignore[operator]


def test_one_to_one_checks_both_sides(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    from writer_service.ontology.spec import RelationSpec

    heads = with_relations(spec, RelationSpec("SUCCEEDS", "Filing", "Filing", "one_to_one"))

    def succ(a: str, b: str) -> CandidateRelation:
        return CandidateRelation(
            relation="SUCCEEDS",
            source=EntityRef(class_name="Filing", key=a),
            target=EntityRef(class_name="Filing", key=b),
        )

    graph.add_edge("SUCCEEDS", ("Filing", "filing_id", "F2"), ("Filing", "filing_id", "F1"))
    (fwd,) = check_batch([succ("F2", "F9")], heads, graph)  # F2 already has a target
    assert fwd.status == "conflict" and "already points to" in fwd.conflict  # type: ignore[operator]
    (back,) = check_batch([succ("F3", "F1")], heads, graph)  # F1 already has a source
    assert back.status == "conflict" and "already has source" in back.conflict  # type: ignore[operator]
    batch = check_batch([succ("A", "B"), succ("C", "B")], heads, InMemoryGraphView())
    assert [r.status for r in batch] == ["pass", "conflict"]
