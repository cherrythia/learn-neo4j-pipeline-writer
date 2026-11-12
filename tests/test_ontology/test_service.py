import pytest
from sqlalchemy import Engine, select

from writer_service.governance.models import AuditLog, OntologyClass, OntologyVersion
from writer_service.governance.uow import UnitOfWork
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyError, OntologyService
from writer_service.ontology.spec import ClassSpec, PropertySpec


@pytest.fixture
def graph() -> InMemoryGraphSchema:
    return InMemoryGraphSchema()


@pytest.fixture
def svc(engine: Engine, graph: InMemoryGraphSchema) -> OntologyService:
    return OntologyService(engine, graph)


def statuses(engine: Engine) -> dict[int, str]:
    with UnitOfWork(engine) as uow:
        return {v.version: v.status for v in uow.ontology_versions.list()}


def test_bootstrap_seeds_and_publishes_core(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    assert svc.bootstrap("admin") == 1
    assert statuses(engine) == {1: "published"}
    assert "wsc_hardwareasset_asset_tag" in graph.names
    spec = svc.published_spec()
    assert spec is not None
    hardware = next(c for c in spec.classes if c.name == "HardwareAsset")
    assert hardware.parent == "Asset"


def test_bootstrap_is_idempotent(svc: OntologyService, engine: Engine) -> None:
    svc.bootstrap("admin")
    assert svc.bootstrap("admin") == 1
    assert statuses(engine) == {1: "published"}


def test_new_draft_copies_published_including_taxonomy(
    svc: OntologyService, engine: Engine
) -> None:
    svc.bootstrap("admin")
    assert svc.new_draft("admin") == 2
    with UnitOfWork(engine) as uow:
        v2 = uow.ontology_versions.get_by_number(2)
        assert v2 is not None and v2.status == "draft"
        hardware = next(c for c in v2.classes if c.name == "HardwareAsset")
        assert hardware.parent is not None and hardware.parent.version_id == v2.id  # remapped
        assert {p.name for p in hardware.properties} >= {"asset_tag", "name", "hardware_type"}
        assert len(v2.relation_types) == len(uow.ontology_versions.get_by_number(1).relation_types)  # type: ignore[union-attr]


def test_only_one_draft_at_a_time(svc: OntologyService) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    with pytest.raises(OntologyError, match="draft v2 already exists"):
        svc.new_draft("admin")


def add_class(engine: Engine, version: int, spec: ClassSpec) -> None:
    with UnitOfWork(engine) as uow:
        v = uow.ontology_versions.get_by_number(version)
        assert v is not None
        c = OntologyClass(
            version=v, namespace=spec.namespace, name=spec.name, identity_key=spec.identity_key
        )
        from writer_service.governance.models import PropertyDef

        c.properties = [
            PropertyDef(name=p.name, datatype=p.datatype, required=p.required)
            for p in spec.properties
        ]
        uow.session.add(c)


def test_publish_supersedes_previous_and_syncs_constraints(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    add_class(
        engine,
        2,
        ClassSpec(
            "Warranty",
            "warranty_id",
            (PropertySpec("warranty_id", "string", True),),
            namespace="ext",
        ),
    )
    svc.publish(2, "alice")
    assert statuses(engine) == {1: "superseded", 2: "published"}
    assert "wsc_ext_warranty_warranty_id" in graph.names
    with UnitOfWork(engine) as uow:
        actions = [(a.actor, a.action) for a in uow.session.scalars(select(AuditLog))]
    assert ("alice", "ontology.publish") in actions


def test_invalid_draft_is_not_published_and_graph_untouched(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    add_class(engine, 2, ClassSpec("Broken", "missing", (PropertySpec("id", "string", True),)))
    before = list(graph.executed)
    with pytest.raises(OntologyError, match="identity key 'missing'"):
        svc.publish(2, "alice")
    assert statuses(engine) == {1: "published", 2: "draft"}
    assert graph.executed == before


def test_graph_failure_rolls_back_the_publish(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    add_class(
        engine,
        2,
        ClassSpec(
            "Warranty",
            "warranty_id",
            (PropertySpec("warranty_id", "string", True),),
            namespace="ext",
        ),
    )
    graph.fail_next = True  # the new class needs a CREATE CONSTRAINT, which fails
    with pytest.raises(RuntimeError, match="neo4j down"):
        svc.publish(2, "alice")
    assert statuses(engine) == {1: "published", 2: "draft"}


def test_publish_requires_a_draft(svc: OntologyService) -> None:
    svc.bootstrap("admin")
    with pytest.raises(OntologyError, match="not a draft"):
        svc.publish(1, "alice")
    with pytest.raises(OntologyError, match="no version 9"):
        svc.publish(9, "alice")


def test_rollback_restores_previous_version_and_constraints(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    add_class(
        engine,
        2,
        ClassSpec(
            "Warranty",
            "warranty_id",
            (PropertySpec("warranty_id", "string", True),),
            namespace="ext",
        ),
    )
    svc.publish(2, "alice")
    assert svc.rollback("bob") == 1
    assert statuses(engine) == {1: "published", 2: "deprecated"}
    assert "wsc_ext_warranty_warranty_id" not in graph.names
    assert "wsc_hardwareasset_asset_tag" in graph.names
    with UnitOfWork(engine) as uow:
        actions = [(a.actor, a.action) for a in uow.session.scalars(select(AuditLog))]
    assert ("bob", "ontology.rollback") in actions


def test_rollback_without_previous_version_fails(svc: OntologyService) -> None:
    svc.bootstrap("admin")
    with pytest.raises(OntologyError, match="nothing to roll back to"):
        svc.rollback("bob")


def test_published_spec_none_before_bootstrap(svc: OntologyService, engine: Engine) -> None:
    assert svc.published_spec() is None
    with UnitOfWork(engine) as uow:
        assert uow.session.scalars(select(OntologyVersion)).all() == []


def deprecate_application_add_warranty(engine: Engine) -> None:
    """Draft v2: deprecate Application (and its relations), add an ext class."""
    with UnitOfWork(engine) as uow:
        v2 = uow.ontology_versions.get_by_number(2)
        assert v2 is not None
        for c in v2.classes:
            if c.name == "Application":
                c.deprecated = True
        for r in v2.relation_types:
            if "Application" in (r.domain_class, r.range_class):
                r.deprecated = True
    add_class(
        engine,
        2,
        ClassSpec(
            "Warranty",
            "warranty_id",
            (PropertySpec("warranty_id", "string", True),),
            namespace="ext",
        ),
    )


def v1_constraints(svc: OntologyService) -> set[str]:
    from writer_service.ontology.neo4j_schema import desired_constraints

    spec = svc.published_spec()
    assert spec is not None
    return set(desired_constraints(spec))


@pytest.mark.parametrize(
    "fail_on",
    ["CREATE CONSTRAINT wsc_ext_warranty", "DROP CONSTRAINT wsc_application_app_code"],
)
def test_partial_graph_failure_restores_published_constraints(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema, fail_on: str
) -> None:
    svc.bootstrap("admin")
    expected = v1_constraints(svc)
    svc.new_draft("admin")
    deprecate_application_add_warranty(engine)
    graph.fail_on = fail_on
    with pytest.raises(RuntimeError, match="neo4j down"):
        svc.publish(2, "alice")
    assert statuses(engine) == {1: "published", 2: "draft"}
    managed = {n for n in graph.names if n.startswith("wsc_")}
    assert managed == expected  # v1's constraints, nothing missing, nothing extra
    svc.publish(2, "alice")  # a retry succeeds
    assert "wsc_application_app_code" not in graph.names


def test_sync_creates_before_dropping(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    svc.bootstrap("admin")
    svc.new_draft("admin")
    deprecate_application_add_warranty(engine)
    graph.executed.clear()
    svc.publish(2, "alice")
    verbs = [s.split()[0] for s in graph.executed]
    assert verbs == sorted(verbs, key=lambda v: v != "CREATE")  # all CREATEs, then DROPs


def test_lock_all_uses_select_for_update_on_mysql(engine: Engine) -> None:
    from sqlalchemy.dialects import mysql
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        captured: list[str] = []
        from writer_service.governance.repositories import OntologyVersionRepository

        repo = OntologyVersionRepository(s)
        real_scalars = s.scalars

        def spy(stmt, *a, **kw):  # type: ignore[no-untyped-def]
            captured.append(str(stmt.compile(dialect=mysql.dialect())))
            return real_scalars(stmt, *a, **kw)

        s.scalars = spy  # type: ignore[method-assign]
        repo.lock_all()
    assert "FOR UPDATE" in captured[0]


def test_commit_failure_after_sync_restores_published_constraints(
    svc: OntologyService,
    engine: Engine,
    graph: InMemoryGraphSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import Session

    svc.bootstrap("admin")
    expected = v1_constraints(svc)
    svc.new_draft("admin")
    deprecate_application_add_warranty(engine)

    def fail_commit(self: Session) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        svc.publish(2, "alice")
    monkeypatch.undo()
    assert statuses(engine) == {1: "published", 2: "draft"}
    assert {n for n in graph.names if n.startswith("wsc_")} == expected


def test_rollback_graph_failure_keeps_current_version_and_constraints(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    from writer_service.ontology.neo4j_schema import desired_constraints

    svc.bootstrap("admin")
    svc.new_draft("admin")
    deprecate_application_add_warranty(engine)
    svc.publish(2, "alice")
    spec2 = svc.published_spec()
    assert spec2 is not None
    v2_constraints = set(desired_constraints(spec2))
    graph.fail_on = "CREATE CONSTRAINT wsc_application_app_code"  # rollback must re-create it
    with pytest.raises(RuntimeError, match="neo4j down"):
        svc.rollback("bob")
    assert statuses(engine) == {1: "superseded", 2: "published"}
    assert {n for n in graph.names if n.startswith("wsc_")} == v2_constraints


def test_rollback_commit_failure_restores_current_constraints(
    svc: OntologyService,
    engine: Engine,
    graph: InMemoryGraphSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import Session

    from writer_service.ontology.neo4j_schema import desired_constraints

    svc.bootstrap("admin")
    svc.new_draft("admin")
    deprecate_application_add_warranty(engine)
    svc.publish(2, "alice")
    spec2 = svc.published_spec()
    assert spec2 is not None

    def fail_commit(self: Session) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        svc.rollback("bob")
    monkeypatch.undo()
    assert statuses(engine) == {1: "superseded", 2: "published"}
    assert {n for n in graph.names if n.startswith("wsc_")} == set(desired_constraints(spec2))


def test_publish_creates_fulltext_indexes(svc: OntologyService, graph: InMemoryGraphSchema) -> None:
    svc.bootstrap("admin")
    assert "wsf_department_dept_code_name" in graph.names  # #28


def test_sync_reapplies_the_published_schema(
    svc: OntologyService, engine: Engine, graph: InMemoryGraphSchema
) -> None:
    """#28: an existing deployment gets the new full-text indexes without a publish."""
    svc.bootstrap("admin")
    graph.names.discard("wsf_department_dept_code_name")  # as if published before #28
    result = svc.sync("admin")
    assert result.created == ["wsf_department_dept_code_name"] and result.dropped == []
    assert "wsf_department_dept_code_name" in graph.names
    with UnitOfWork(engine) as uow:
        actions = [a.action for a in uow.session.scalars(select(AuditLog))]
    assert actions[-1] == "ontology.sync"


def test_sync_without_a_published_version_is_an_error(svc: OntologyService) -> None:
    with pytest.raises(OntologyError, match="nothing is published"):
        svc.sync("admin")
