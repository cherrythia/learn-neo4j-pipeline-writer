import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from writer_service.governance.models import (
    AuditLog,
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


def version(session: Session, n: int = 1) -> OntologyVersion:
    v = OntologyVersion(version=n, status="draft", created_by="admin")
    session.add(v)
    session.flush()
    return v


def test_ontology_class_with_properties_and_taxonomy(session: Session) -> None:
    v = version(session)
    org = OntologyClass(version=v, namespace="core", name="Organisation", identity_key="name")
    issuer = OntologyClass(
        version=v, namespace="core", name="Issuer", identity_key="ticker", parent=org
    )
    issuer.properties = [
        PropertyDef(name="ticker", datatype="string", required=True),
        PropertyDef(name="board", datatype="string"),
    ]
    session.add_all([org, issuer])
    session.commit()
    got = session.scalars(select(OntologyClass).where(OntologyClass.name == "Issuer")).one()
    assert got.parent is not None and got.parent.name == "Organisation"
    assert {p.name for p in got.properties} == {"ticker", "board"}
    assert got.deprecated is False


def test_class_name_unique_per_version_and_namespace(session: Session) -> None:
    v = version(session)
    session.add(OntologyClass(version=v, namespace="core", name="Issuer", identity_key="ticker"))
    session.add(OntologyClass(version=v, namespace="ext", name="Issuer", identity_key="ticker"))
    session.flush()  # same name in another namespace is fine
    session.add(OntologyClass(version=v, namespace="core", name="Issuer", identity_key="id"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_version_number_unique(session: Session) -> None:
    version(session, 1)
    with pytest.raises(IntegrityError):
        version(session, 1)


def test_property_name_unique_per_class(session: Session) -> None:
    v = version(session)
    c = OntologyClass(version=v, namespace="core", name="Rule", identity_key="code")
    c.properties = [
        PropertyDef(name="code", datatype="string"),
        PropertyDef(name="code", datatype="string"),
    ]
    session.add(c)
    with pytest.raises(IntegrityError):
        session.flush()


def test_invalid_enum_value_rejected(session: Session) -> None:
    session.add(OntologyVersion(version=9, status="bogus", created_by="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_relation_type(session: Session) -> None:
    v = version(session)
    session.add(
        RelationType(
            version=v,
            namespace="core",
            name="FILED_BY",
            domain_class="Filing",
            range_class="Issuer",
            cardinality="many_to_one",
        )
    )
    session.commit()
    assert session.scalars(select(RelationType)).one().cardinality == "many_to_one"


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(Candidate(run_id="no-such-run", kind="entity", canonical_hash="h", payload={}))
    with pytest.raises(IntegrityError):
        session.flush()


def test_run_candidates_and_review_item(session: Session) -> None:
    run = IngestionRun(source_type="pdf", source_ref="samples/a.pdf", ontology_version=1)
    cand = Candidate(
        run=run,
        kind="relation",
        canonical_hash="abc",
        payload={"type": "FILED_BY"},
        evidence=[{"page": 1, "span": "..."}],
        score=0.7,
        breakdown={"grounding": 0.9},
        decision="review",
    )
    cand.review_item = ReviewItem(reason="score")
    session.add(run)
    session.commit()
    assert run.status == "running"
    assert run.id and len(run.id) == 36
    assert session.scalars(select(ReviewItem)).one().status == "open"
    assert run.candidates[0].review_item is not None


def test_one_review_item_per_candidate(session: Session) -> None:
    run = IngestionRun(source_type="excel", source_ref="x.xlsx", ontology_version=1)
    cand = Candidate(run=run, kind="entity", canonical_hash="h", payload={})
    session.add(run)
    session.flush()
    session.add_all(
        [
            ReviewItem(candidate=cand, reason="score"),
            ReviewItem(candidate_id=cand.id, reason="conflict"),
        ]
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_extension_proposal_defaults(session: Session) -> None:
    p = ExtensionProposal(kind="class", payload={"name": "ESGMetric"}, proposed_by="llm")
    session.add(p)
    session.commit()
    assert p.status == "pending" and p.created_at is not None


def test_scoring_config_json_roundtrip(session: Session) -> None:
    weights = {"grounding": 0.25, "back_translation": 0.2}
    session.add(
        ScoringConfig(
            weights=weights,
            commit_threshold=0.85,
            review_threshold=0.6,
            self_consistency_runs=3,
            created_by="admin",
        )
    )
    session.commit()
    assert session.scalars(select(ScoringConfig)).one().weights == weights


def test_column_mapping_and_user_and_audit(session: Session) -> None:
    session.add(
        ColumnMapping(
            dataset_id=1, dataset_name="issuers", mapping={"rows": []}, suggested_by="llm"
        )
    )
    session.add(User(username="alice", password_hash="x", role="reviewer"))
    session.add(
        AuditLog(actor="alice", action="review.approve", entity_type="review_item", entity_id="1")
    )
    session.commit()
    assert session.scalars(select(ColumnMapping)).one().status == "draft"
    with pytest.raises(IntegrityError):
        session.add(User(username="alice", password_hash="y", role="admin"))
        session.flush()
