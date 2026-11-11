"""Load and activate the scoring policy stored in the governance database."""

from __future__ import annotations

from sqlalchemy import update

from writer_service.governance.models import ScoringConfig
from writer_service.governance.uow import UnitOfWork
from writer_service.scoring.policy import ScoringPolicy, Weights, default_policy


def active_policy(uow: UnitOfWork) -> ScoringPolicy:
    """The active policy, or the design default when none has been stored yet."""
    row = uow.scoring_configs.active()
    if row is None:
        return default_policy()
    return ScoringPolicy(
        weights=Weights.model_validate(row.weights),
        commit_threshold=row.commit_threshold,
        review_threshold=row.review_threshold,
        source_reliability=row.source_reliability or default_policy().source_reliability,
        self_consistency_runs=row.self_consistency_runs,
    )


def activate_policy(uow: UnitOfWork, policy: ScoringPolicy, *, actor: str) -> ScoringConfig:
    """Store ``policy`` as a new active row (history is kept) and audit the change."""
    uow.session.execute(update(ScoringConfig).values(active=False))
    row = ScoringConfig(
        weights=policy.weights.model_dump(),
        commit_threshold=policy.commit_threshold,
        review_threshold=policy.review_threshold,
        source_reliability=dict(policy.source_reliability),
        self_consistency_runs=policy.self_consistency_runs,
        active=True,
        created_by=actor,
    )
    uow.session.add(row)
    uow.session.flush()
    uow.audit(
        actor, "scoring.activate", "scoring_config", str(row.id), policy.model_dump(mode="json")
    )
    return row
