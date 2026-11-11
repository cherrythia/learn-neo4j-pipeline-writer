import pytest
from sqlalchemy import Engine

from writer_service.governance.models import OntologyVersion
from writer_service.governance.uow import UnitOfWork


def test_commit_on_success(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        uow.session.add(OntologyVersion(version=1, status="draft", created_by="a"))
    with UnitOfWork(engine) as uow:
        assert uow.ontology_versions.get_by_number(1) is not None


def test_rollback_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError), UnitOfWork(engine) as uow:
        uow.session.add(OntologyVersion(version=2, status="draft", created_by="a"))
        uow.session.flush()
        raise RuntimeError("boom")
    with UnitOfWork(engine) as uow:
        assert uow.ontology_versions.get_by_number(2) is None


def test_latest_published(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        uow.session.add_all(
            [
                OntologyVersion(version=1, status="superseded", created_by="a"),
                OntologyVersion(version=2, status="published", created_by="a"),
                OntologyVersion(version=3, status="draft", created_by="a"),
            ]
        )
    with UnitOfWork(engine) as uow:
        pub = uow.ontology_versions.published()
        assert pub is not None and pub.version == 2


def test_audit_helper_records_actor(engine: Engine) -> None:
    with UnitOfWork(engine) as uow:
        uow.audit("alice", "ontology.publish", "ontology_version", "2", {"from": 1})
    with UnitOfWork(engine) as uow:
        (entry,) = uow.audit_log.list()
        assert entry.actor == "alice" and entry.detail == {"from": 1}
