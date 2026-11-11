"""Create / read / update / delete and constraint checks for every governance model."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from writer_service.governance.models import (
    AuditLog,
    Base,
    Candidate,
    ColumnMapping,
    ExtensionProposal,
    IngestionRun,
    OntologyClass,
    OntologyVersion,
    PropertyDef,
    RelationType,
    ReviewItem,
    ScoringConfig,
    User,
)
from writer_service.governance.uow import UnitOfWork

# --------------------------------------------------------------------------- factories


def _version(s: Session) -> OntologyVersion:
    v = OntologyVersion(version=len(s.scalars(select(OntologyVersion)).all()) + 100, created_by="t")
    s.add(v)
    s.flush()
    return v


def _class(s: Session) -> OntologyClass:
    c = OntologyClass(version=_version(s), name="Issuer", identity_key="ticker")
    s.add(c)
    return c


def _run(s: Session) -> IngestionRun:
    r = IngestionRun(source_type="pdf", source_ref="a.pdf", ontology_version=1)
    s.add(r)
    s.flush()
    return r


def _candidate(s: Session) -> Candidate:
    c = Candidate(run=_run(s), kind="entity", canonical_hash="h", payload={})
    s.add(c)
    return c


FACTORIES: dict[type[Base], tuple[Callable[[Session], Any], str, object]] = {
    # model: (factory, attribute to update, new value)
    OntologyVersion: (_version, "notes", "updated"),
    OntologyClass: (_class, "description", "updated"),
    PropertyDef: (lambda s: PropertyDef(ontology_class=_class(s), name="p"), "required", True),
    RelationType: (
        lambda s: RelationType(version=_version(s), name="R", domain_class="A", range_class="B"),
        "cardinality",
        "one_to_one",
    ),
    ExtensionProposal: (
        lambda s: ExtensionProposal(kind="class", payload={}, proposed_by="llm"),
        "status",
        "approved",
    ),
    ScoringConfig: (
        lambda s: ScoringConfig(
            weights={}, commit_threshold=0.85, review_threshold=0.6, created_by="t"
        ),
        "active",
        True,
    ),
    IngestionRun: (_run, "status", "completed"),
    Candidate: (_candidate, "decision", "commit"),
    ReviewItem: (
        lambda s: ReviewItem(candidate=_candidate(s), reason="score"),
        "status",
        "approved",
    ),
    ColumnMapping: (
        lambda s: ColumnMapping(dataset_id=1, dataset_name="d", mapping={}, suggested_by="llm"),
        "status",
        "approved",
    ),
    User: (lambda s: User(username="u", password_hash="x", role="reviewer"), "role", "admin"),
    AuditLog: (
        lambda s: AuditLog(actor="a", action="x", entity_type="t", entity_id="1"),
        "detail",
        {"k": "v"},
    ),
}

MODELS = list(FACTORIES)


def test_every_model_is_covered() -> None:
    mapped = {m.class_ for m in Base.registry.mappers}
    assert mapped == set(FACTORIES)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_create_read_update_delete(session: Session, model: type[Base]) -> None:
    factory, attr, new_value = FACTORIES[model]
    obj = factory(session)
    session.add(obj)
    session.commit()
    pk = obj.id  # type: ignore[attr-defined]

    session.expire_all()
    loaded = session.get(model, pk)
    assert loaded is not None

    setattr(loaded, attr, new_value)
    session.commit()
    session.expire_all()
    assert getattr(session.get(model, pk), attr) == new_value

    session.delete(session.get(model, pk))
    session.commit()
    assert session.get(model, pk) is None


# --------------------------------------------------------------------------- enums

ENUM_CASES = [
    (OntologyVersion, "status"),
    (OntologyClass, "namespace"),
    (PropertyDef, "datatype"),
    (RelationType, "namespace"),
    (RelationType, "cardinality"),
    (ExtensionProposal, "kind"),
    (ExtensionProposal, "status"),
    (ExtensionProposal, "proposed_by"),
    (IngestionRun, "source_type"),
    (IngestionRun, "status"),
    (Candidate, "kind"),
    (Candidate, "decision"),
    (ReviewItem, "reason"),
    (ReviewItem, "status"),
    (ColumnMapping, "status"),
    (ColumnMapping, "suggested_by"),
    (User, "role"),
]


@pytest.mark.parametrize("model,column", ENUM_CASES, ids=lambda x: getattr(x, "__name__", x))
def test_database_rejects_invalid_enum_value(
    engine: Engine, session: Session, model: type[Base], column: str
) -> None:
    factory, _, _ = FACTORIES[model]
    obj = factory(session)
    session.add(obj)
    session.commit()
    table = model.__tablename__
    # Bypass the ORM so the database's CHECK constraint is what gets tested.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET {column} = 'bogus' WHERE id = :id"), {"id": obj.id})  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- uniqueness / FKs


def test_relation_type_unique_per_version_namespace(session: Session) -> None:
    v = _version(session)
    session.add(RelationType(version=v, name="R", domain_class="A", range_class="B"))
    session.flush()
    session.add(RelationType(version=v, name="R", domain_class="C", range_class="D"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_proposal_source_run_must_exist(session: Session) -> None:
    session.add(
        ExtensionProposal(kind="class", payload={}, proposed_by="llm", source_run_id="nope")
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_version_cascades_to_classes_properties_relations(session: Session) -> None:
    v = _version(session)
    c = OntologyClass(version=v, name="Issuer", identity_key="ticker")
    c.properties = [PropertyDef(name="ticker")]
    session.add_all([c, RelationType(version=v, name="R", domain_class="A", range_class="B")])
    session.commit()
    session.delete(v)
    session.commit()
    for model in (OntologyClass, PropertyDef, RelationType):
        assert session.scalars(select(model)).all() == []


def test_removing_property_from_class_deletes_it(session: Session) -> None:
    c = _class(session)
    c.properties = [PropertyDef(name="a"), PropertyDef(name="b")]
    session.commit()
    c.properties.pop()
    session.commit()
    assert [p.name for p in session.scalars(select(PropertyDef))] == ["a"]


# --------------------------------------------------------------------------- timestamps


def test_timestamps_come_back_timezone_aware_utc(session: Session) -> None:
    sgt = timezone(timedelta(hours=8))
    run = _run(session)
    run.finished_at = datetime(2026, 1, 1, 8, 0, tzinfo=sgt)
    session.commit()
    session.expire_all()
    loaded = session.get(IngestionRun, run.id)
    assert loaded is not None and loaded.finished_at is not None
    assert loaded.finished_at.tzinfo is UTC
    assert loaded.finished_at == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert loaded.started_at < datetime.now(UTC)  # comparable with aware values


def test_naive_datetime_is_rejected(session: Session) -> None:
    run = _run(session)
    run.finished_at = datetime(2026, 1, 1)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(StatementError, match="naive datetime"):
        session.flush()


# --------------------------------------------------------------------------- repositories


def test_published_is_deterministic_when_several_are_published(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        for n in (1, 3, 2):
            uow.session.add(OntologyVersion(version=n, status="published", created_by="t"))
    with UnitOfWork(engine) as uow:
        pub = uow.ontology_versions.published()
        assert pub is not None and pub.version == 3


def test_active_scoring_config(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        assert uow.scoring_configs.active() is None
        for active in (True, False, True):
            uow.session.add(
                ScoringConfig(
                    weights={"n": active},
                    commit_threshold=0.85,
                    review_threshold=0.6,
                    created_by="t",
                    active=active,
                )
            )
    with UnitOfWork(engine) as uow:
        cfg = uow.scoring_configs.active()
        assert cfg is not None and cfg.id == 3


def test_raw_sql_version_delete_cascades_through_class_hierarchy(
    engine: Engine, session: Session
) -> None:
    """The database-level cascade must work even when classes have parents (no ORM help)."""
    v = _version(session)
    parent = OntologyClass(version=v, name="Organisation", identity_key="name")
    child = OntologyClass(version=v, name="Issuer", identity_key="ticker", parent=parent)
    child.properties = [PropertyDef(name="ticker")]
    session.add_all([parent, child])
    session.commit()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ontology_versions WHERE id = :id"), {"id": v.id})
    session.expire_all()
    for model in (OntologyClass, PropertyDef):
        assert session.scalars(select(model)).all() == []


def test_deleting_parent_class_keeps_child(session: Session) -> None:
    v = _version(session)
    parent = OntologyClass(version=v, name="Organisation", identity_key="name")
    child = OntologyClass(version=v, name="Issuer", identity_key="ticker", parent=parent)
    session.add_all([parent, child])
    session.commit()
    session.delete(parent)
    session.commit()
    session.expire_all()
    remaining = session.scalars(select(OntologyClass)).one()
    assert remaining.name == "Issuer" and remaining.parent_id is None
