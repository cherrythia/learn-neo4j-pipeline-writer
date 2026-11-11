from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import downgrade, upgrade
from writer_service.governance.models import Base

EXPECTED_TABLES = {
    "ontology_versions",
    "ontology_classes",
    "property_defs",
    "relation_types",
    "extension_proposals",
    "scoring_configs",
    "ingestion_runs",
    "candidates",
    "review_items",
    "column_mappings",
    "users",
    "audit_log",
}


def test_upgrade_creates_all_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    tables = set(inspect(create_engine_for(url)).get_table_names())
    assert tables >= EXPECTED_TABLES


def test_models_match_migrations(tmp_path: Path) -> None:
    """Fails if someone changes a model without writing a migration."""
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    with create_engine_for(url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    downgrade(url, "base")
    eng = create_engine_for(url)
    assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    eng.dispose()
    upgrade(url)
    assert set(inspect(create_engine_for(url)).get_table_names()) >= EXPECTED_TABLES
