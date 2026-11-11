"""Small repositories over the governance models. Add query methods as features need them."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from writer_service.governance.models import AuditLog, Base, OntologyVersion, ScoringConfig


class Repository[T: Base]:
    model: type[T]

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, obj: T) -> T:
        self.session.add(obj)
        return obj

    def get(self, pk: Any) -> T | None:
        return self.session.get(self.model, pk)

    def list(self) -> list[T]:
        return list(self.session.scalars(select(self.model)))


class OntologyVersionRepository(Repository[OntologyVersion]):
    model = OntologyVersion

    def get_by_number(self, version: int) -> OntologyVersion | None:
        stmt = select(OntologyVersion).where(OntologyVersion.version == version)
        return self.session.scalars(stmt).one_or_none()

    def lock_all(self) -> list[OntologyVersion]:
        """All versions, row-locked until the transaction ends (``FOR UPDATE``; no-op on SQLite).

        Serialises publish / rollback / new-draft across processes.
        """
        stmt = select(OntologyVersion).order_by(OntologyVersion.version).with_for_update()
        return list(self.session.scalars(stmt))

    def published(self) -> OntologyVersion | None:
        """The current published version.

        Publishing (issue #4) supersedes the previous version in the same transaction, so
        normally only one row is ``published``; if several ever are, the highest version wins
        rather than raising.
        """
        stmt = (
            select(OntologyVersion)
            .where(OntologyVersion.status == "published")
            .order_by(OntologyVersion.version.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class ScoringConfigRepository(Repository[ScoringConfig]):
    model = ScoringConfig

    def active(self) -> ScoringConfig | None:
        """The active scoring config (newest active row if several are flagged)."""
        stmt = (
            select(ScoringConfig)
            .where(ScoringConfig.active.is_(True))
            .order_by(ScoringConfig.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class AuditLogRepository(Repository[AuditLog]):
    model = AuditLog
