from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef
from writer_service.pipeline.gates import check_batch
from writer_service.pipeline.graph_view import InMemoryGraphView


def ent(cls: str, **props: object) -> CandidateEntity:
    return CandidateEntity(class_name=cls, properties=props)  # type: ignore[arg-type]


def assigned_to(asset_tag: str, dept_code: str) -> CandidateRelation:
    return CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=asset_tag),
        target=EntityRef(class_name="Department", key=dept_code),
    )


def test_clean_candidates_pass(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    results = check_batch(
        [ent("Department", dept_code="FIN", name="Finance"), assigned_to("HW-25-0101", "FIN")],
        spec,
        graph,
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
        "Application",
        "app_code",
        "APP-ERP",
        {"app_code": "APP-ERP", "name": "Finance ERP", "criticality": "high"},
    )
    (r,) = check_batch(
        [ent("Application", app_code="app-erp", name="finance erp", criticality="low")],
        spec,
        graph,
    )
    assert r.status == "conflict"
    assert r.conflict == (
        "Application 'app-erp': property 'criticality' is 'high' in the graph, candidate says 'low'"
    )


def test_same_values_with_different_spacing_or_case_do_not_conflict(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    graph.add_node("Department", "dept_code", "FIN", {"dept_code": "FIN", "name": "Finance Ops"})
    (r,) = check_batch([ent("Department", dept_code="FIN", name=" finance  ops")], spec, graph)
    assert r.status == "pass"


def test_many_to_one_second_target_in_graph_is_conflict(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    graph.add_edge(
        "ASSIGNED_TO",
        ("HardwareAsset", "asset_tag", "HW-25-0101"),
        ("Department", "dept_code", "FIN"),
    )
    (same,) = check_batch([assigned_to("hw-25-0101", "FIN")], spec, graph)
    assert same.status == "pass"  # re-asserting the same fact is fine
    (other,) = check_batch([assigned_to("HW-25-0101", "HR")], spec, graph)
    assert other.status == "conflict"
    assert "ASSIGNED_TO is many_to_one" in other.conflict  # type: ignore[operator]
    assert "already points to Department 'FIN'" in other.conflict  # type: ignore[operator]


def test_many_to_one_contradiction_within_batch(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    results = check_batch([assigned_to("HW1", "FIN"), assigned_to("HW1", "HR")], spec, graph)
    assert [r.status for r in results] == ["pass", "conflict"]
    assert "in this batch" in results[1].conflict  # type: ignore[operator]


def test_many_to_one_licenses_and_owned_by_and_billed_on(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    """Every many_to_one core relation is gated, not only ASSIGNED_TO."""

    def r(name: str, src: tuple[str, str], tgt: tuple[str, str]) -> CandidateRelation:
        return CandidateRelation(
            relation=name,
            source=EntityRef(class_name=src[0], key=src[1]),
            target=EntityRef(class_name=tgt[0], key=tgt[1]),
        )

    results = check_batch(
        [
            r("LICENSES", ("SoftwareLicense", "SL-1"), ("Software", "SW-QDOC")),
            r("LICENSES", ("SoftwareLicense", "SL-1"), ("Software", "SW-OTHER")),
            r("OWNED_BY", ("Application", "APP-ERP"), ("Department", "FIN")),
            r("OWNED_BY", ("Application", "APP-ERP"), ("Department", "HR")),
            r("BILLED_ON", ("HardwareAsset", "HW-1"), ("Invoice", "INV-2025-0418")),
            r("BILLED_ON", ("HardwareAsset", "HW-1"), ("Invoice", "INV-2025-0419")),
        ],
        spec,
        graph,
    )
    assert [x.status for x in results] == ["pass", "conflict"] * 3


def test_property_contradiction_within_batch(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    results = check_batch(
        [
            ent("Application", app_code="APP-ERP", name="A", criticality="high"),
            ent("Application", app_code="APP-ERP", name="A", criticality="low"),
        ],
        spec,
        graph,
    )
    assert [r.status for r in results] == ["pass", "conflict"]


def test_many_to_many_allows_multiple_targets(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    installs = [
        CandidateRelation(
            relation="INSTALLED_ON",
            source=EntityRef(class_name="Software", key="SW-QDOC"),
            target=EntityRef(class_name="HardwareAsset", key=tag),
        )
        for tag in ("HW-25-0101", "HW-25-0102")
    ]
    assert [r.status for r in check_batch(installs, spec, graph)] == ["pass", "pass"]


def test_one_to_many_checks_the_source_side(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    from writer_service.ontology.spec import RelationSpec

    holds = OntologySpec(
        spec.classes,
        (*spec.relations, RelationSpec("HOLDS", "Department", "HardwareAsset", "one_to_many")),
    )
    graph.add_edge(
        "HOLDS", ("Department", "dept_code", "FIN"), ("HardwareAsset", "asset_tag", "HW1")
    )
    r = CandidateRelation(
        relation="HOLDS",
        source=EntityRef(class_name="Department", key="HR"),
        target=EntityRef(class_name="HardwareAsset", key="HW1"),
    )
    (res,) = check_batch([r], holds, graph)
    assert res.status == "conflict" and "already has source Department 'FIN'" in res.conflict  # type: ignore[operator]


def with_relations(spec: OntologySpec, *rels: object) -> OntologySpec:
    return OntologySpec(spec.classes, (*spec.relations, *rels))  # type: ignore[arg-type]


def test_many_to_one_sees_existing_edge_to_a_subclass_node(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    """Range is Asset; the graph edge points to a HardwareAsset (subclass) node."""
    from writer_service.ontology.spec import RelationSpec

    hosted = with_relations(spec, RelationSpec("HOSTED_ON", "Application", "Asset", "many_to_one"))
    graph.add_edge(
        "HOSTED_ON", ("Application", "app_code", "APP-ERP"), ("HardwareAsset", "asset_tag", "HW1")
    )
    other = CandidateRelation(
        relation="HOSTED_ON",
        source=EntityRef(class_name="Application", key="APP-ERP"),
        target=EntityRef(class_name="Asset", key="HW9"),
    )
    (res,) = check_batch([other], hosted, graph)
    assert res.status == "conflict" and "HardwareAsset 'HW1'" in res.conflict  # type: ignore[operator]
    same = other.model_copy(update={"target": EntityRef(class_name="HardwareAsset", key="hw1")})
    assert check_batch([same], hosted, graph)[0].status == "pass"


def test_many_to_one_sees_existing_edge_from_a_subclass_source(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    """BILLED_ON's domain is Asset: an edge from a SoftwareLicense node is gated like any other."""
    graph.add_edge(
        "BILLED_ON",
        ("SoftwareLicense", "asset_tag", "SL-1"),
        ("Invoice", "invoice_number", "INV-2025-0418"),
    )
    r = CandidateRelation(
        relation="BILLED_ON",
        source=EntityRef(class_name="SoftwareLicense", key="sl-1"),
        target=EntityRef(class_name="Invoice", key="INV-2025-0419"),
    )
    (res,) = check_batch([r], spec, graph)
    assert res.status == "conflict" and "Invoice 'INV-2025-0418'" in res.conflict  # type: ignore[operator]


def test_one_to_many_contradiction_within_batch(
    spec: OntologySpec, graph: InMemoryGraphView
) -> None:
    from writer_service.ontology.spec import RelationSpec

    holds = with_relations(
        spec, RelationSpec("HOLDS", "Department", "HardwareAsset", "one_to_many")
    )
    rels = [
        CandidateRelation(
            relation="HOLDS",
            source=EntityRef(class_name="Department", key=src),
            target=EntityRef(class_name="HardwareAsset", key="HW1"),
        )
        for src in ("FIN", "HR")
    ]
    results = check_batch(rels, holds, graph)
    assert [r.status for r in results] == ["pass", "conflict"]
    assert "has source Department 'FIN' in this batch" in results[1].conflict  # type: ignore[operator]


def test_one_to_one_checks_both_sides(spec: OntologySpec, graph: InMemoryGraphView) -> None:
    from writer_service.ontology.spec import RelationSpec

    replaces = with_relations(
        spec, RelationSpec("REPLACES", "HardwareAsset", "HardwareAsset", "one_to_one")
    )

    def succ(a: str, b: str) -> CandidateRelation:
        return CandidateRelation(
            relation="REPLACES",
            source=EntityRef(class_name="HardwareAsset", key=a),
            target=EntityRef(class_name="HardwareAsset", key=b),
        )

    graph.add_edge(
        "REPLACES", ("HardwareAsset", "asset_tag", "HW2"), ("HardwareAsset", "asset_tag", "HW1")
    )
    (fwd,) = check_batch([succ("HW2", "HW9")], replaces, graph)  # HW2 already has a target
    assert fwd.status == "conflict" and "already points to" in fwd.conflict  # type: ignore[operator]
    (back,) = check_batch([succ("HW3", "HW1")], replaces, graph)  # HW1 already has a source
    assert back.status == "conflict" and "already has source" in back.conflict  # type: ignore[operator]
    batch = check_batch([succ("A", "B"), succ("C", "B")], replaces, InMemoryGraphView())
    assert [r.status for r in batch] == ["pass", "conflict"]
