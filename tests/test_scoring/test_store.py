from sqlalchemy import Engine, select

from writer_service.governance.models import AuditLog, ScoringConfig
from writer_service.governance.uow import UnitOfWork
from writer_service.scoring.policy import default_policy
from writer_service.scoring.store import activate_policy, active_policy


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
