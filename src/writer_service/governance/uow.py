"""Unit of work: one transaction per business operation; commit on success, else rollback."""

from __future__ import annotations

from types import TracebackType
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from writer_service.governance.db import make_session_factory
from writer_service.governance.models import AuditLog
from writer_service.governance.repositories import AuditLogRepository, OntologyVersionRepository


class UnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self._factory = make_session_factory(engine)

    def __enter__(self) -> UnitOfWork:
        self.session: Session = self._factory()
        self.ontology_versions = OntologyVersionRepository(self.session)
        self.audit_log = AuditLogRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()

    def audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Record who did what, in the same transaction as the change itself."""
        return self.audit_log.add(
            AuditLog(
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                detail=detail or {},
            )
        )
