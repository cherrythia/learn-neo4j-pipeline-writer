"""Runs migrations against a real MySQL (the compose stack). Skipped when not configured.

    WRITER_TEST_MYSQL_URL=mysql+pymysql://writer:<pw>@127.0.0.1:3306/governance \
        uv run pytest -m integration
"""

import os

import pytest
from sqlalchemy import inspect, text

from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import downgrade, upgrade
from writer_service.governance.models import OntologyVersion
from writer_service.governance.uow import UnitOfWork

pytestmark = pytest.mark.integration

URL = os.environ.get("WRITER_TEST_MYSQL_URL")


@pytest.fixture
def mysql_url() -> str:
    if not URL:
        pytest.skip("WRITER_TEST_MYSQL_URL not set")
    return URL


def test_upgrade_downgrade_roundtrip_on_mysql(mysql_url: str) -> None:
    downgrade(mysql_url, "base")
    upgrade(mysql_url)
    engine = create_engine_for(mysql_url)
    try:
        assert "candidates" in inspect(engine).get_table_names()
        with UnitOfWork(engine) as uow:
            uow.session.add(OntologyVersion(version=1, status="draft", created_by="it"))
        with pytest.raises(Exception, match="(?i)check"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ontology_versions (version, status, created_by, created_at) "
                    "VALUES (2, 'bogus', 'it', NOW())"
                )
            )
    finally:
        engine.dispose()
        downgrade(mysql_url, "base")
