"""Small repositories over the governance models. Add query methods as features need them."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from writer_service.governance.models import AuditLog, Base, OntologyVersion


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

    def published(self) -> OntologyVersion | None:
        stmt = select(OntologyVersion).where(OntologyVersion.status == "published")
        return self.session.scalars(stmt).one_or_none()


class AuditLogRepository(Repository[AuditLog]):
    model = AuditLog
