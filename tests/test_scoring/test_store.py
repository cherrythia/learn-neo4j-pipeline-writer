import pytest
from sqlalchemy import Engine, select

from writer_service.governance.models import AuditLog, Candidate, IngestionRun, ScoringConfig
from writer_service.governance.uow import UnitOfWork
from writer_service.scoring.engine import Signals, route, score
from writer_service.scoring.policy import default_policy
from writer_service.scoring.store import activate_policy, active_policy, record_outcome


def test_default_when_nothing_stored(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        assert active_policy(uow) == default_policy()


def test_activate_stores_new_active_row_and_deactivates_others(engine: Engine) -> None:
    custom = default_policy().model_copy(update={"commit_threshold": 0.9})
    with UnitOfWork(engine) as uow:
        activate_policy(uow, default_policy(), actor="alice")
    with UnitOfWork(engine) as uow:
        activate_policy(uow, custom, actor="bob")
    with UnitOfWork(engine) as uow:
        rows = uow.session.scalars(select(ScoringConfig).order_by(ScoringConfig.id)).all()
        assert [r.active for r in rows] == [False, True]
        assert active_policy(uow) == custom
        audit = uow.session.scalars(select(AuditLog)).all()
        assert [(a.actor, a.action) for a in audit] == [
            ("alice", "scoring.activate"),
            ("bob", "scoring.activate"),
        ]
        assert audit[1].detail["commit_threshold"] == 0.9


def test_stored_policy_roundtrips_all_fields(engine: Engine) -> None:
    custom = default_policy().model_copy(
        update={
            "self_consistency_runs": 5,
            "source_reliability": {"pdf": 0.7, "excel": 0.8, "superset": 0.99},
        }
    )
    with UnitOfWork(engine) as uow:
        activate_policy(uow, custom, actor="alice")
    with UnitOfWork(engine) as uow:
        assert active_policy(uow) == custom


def test_legacy_row_without_reliability_falls_back_to_defaults(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        uow.session.add(
            ScoringConfig(
                weights=default_policy().weights.model_dump(),
                commit_threshold=0.8,
                review_threshold=0.5,
                created_by="old",
                active=True,
            )
        )
    with UnitOfWork(engine) as uow:
        p = active_policy(uow)
        assert p.source_reliability == default_policy().source_reliability
        assert p.commit_threshold == 0.8


def _candidate(uow: UnitOfWork) -> Candidate:
    run = IngestionRun(source_type="pdf", source_ref="a.pdf", ontology_version=1)
    cand = Candidate(run=run, kind="relation", canonical_hash="h", payload={})
    uow.session.add(run)
    return cand


def test_record_outcome_stores_breakdown_and_decision(engine: Engine) -> None:
    policy = default_policy()
    s = score(
        Signals(
            source="pdf",
            grounding=0.9,
            back_translation=0.8,
            stability=2 / 3,
            entity_match=None,
            self_confidence=0.9,
        ),
        policy,
    )
    with UnitOfWork(engine) as uow:
        cand = _candidate(uow)
        record_outcome(cand, route(s.value, policy), s)
    with UnitOfWork(engine) as uow:
        stored = uow.session.scalars(select(Candidate)).one()
        assert stored.decision == "review"
        assert stored.score == pytest.approx(s.value)
        assert stored.breakdown["components"]["stability"]["weight"] == 0.15
        assert stored.breakdown["reasons"][0].startswith("score 0.775")
        assert stored.gate_failures == []


def test_record_outcome_for_gate_rejection_has_no_score(engine: Engine) -> None:
    failures = ["schema: unknown class 'Foo'"]
    with UnitOfWork(engine) as uow:
        cand = _candidate(uow)
        record_outcome(
            cand, route(0.0, default_policy(), gate_failures=failures), gate_failures=failures
        )
    with UnitOfWork(engine) as uow:
        stored = uow.session.scalars(select(Candidate)).one()
        assert stored.decision == "reject" and stored.score is None
        assert stored.gate_failures == failures
        assert stored.breakdown == {"decision": "reject", "reasons": failures}


def test_invalid_policy_from_model_copy_is_rejected_and_nothing_written(engine: Engine) -> None:
    from pydantic import ValidationError

    broken = default_policy().model_copy(update={"commit_threshold": 0.5})  # below review 0.60
    with pytest.raises(ValidationError), UnitOfWork(engine) as uow:
        activate_policy(uow, broken, actor="mallory")
    with UnitOfWork(engine) as uow:
        assert uow.session.scalars(select(ScoringConfig)).all() == []
        assert uow.session.scalars(select(AuditLog)).all() == []
        assert active_policy(uow) == default_policy()
