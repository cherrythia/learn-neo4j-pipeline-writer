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


def test_0004_allows_the_duplicate_review_reason(tmp_path: Path) -> None:
    """#38: the CHECK constraint on review_items.reason gains ``duplicate`` (and loses it on
    downgrade)."""
    import sqlite3

    path = tmp_path / "g.sqlite"
    url = f"sqlite:///{path}"

    def reason_check() -> str:
        with sqlite3.connect(path) as conn:
            (sql,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'review_items'"
            ).fetchone()
        return next(line for line in sql.splitlines() if "ck_review_items_review_reason" in line)

    upgrade(url)
    assert "'duplicate'" in reason_check()
    from writer_service.governance.models import Candidate, IngestionRun, ReviewItem
    from writer_service.governance.uow import UnitOfWork

    eng = create_engine_for(url)
    with UnitOfWork(eng) as uow:
        run = IngestionRun(
            source_type="superset", source_ref="d:1", ontology_version=1, started_by="t"
        )
        uow.session.add(run)
        uow.session.flush()
        cand = Candidate(run_id=run.id, kind="entity", canonical_hash="h", payload={}, evidence=[])
        cand.review_item = ReviewItem(reason="duplicate", conflict_with={"duplicate_of": {}})
        uow.session.add(cand)
    eng.dispose()
    downgrade(url, "0003")  # the duplicate row must not violate the old CHECK
    assert "'duplicate'" not in reason_check() and "'conflict'" in reason_check()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT reason FROM review_items").fetchall() == [("score",)]
