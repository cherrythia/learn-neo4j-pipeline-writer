"""DatasetReader against the compose MySQL with the read-only `reader` user, plus the mock
Superset on :8088. Skipped unless WRITER_TEST_SOURCE_DB_URL is set."""

import os

import pytest
from sqlalchemy import create_engine

from demo_env import data
from writer_service.ingestion.superset import DatasetReader, SupersetClient

pytestmark = pytest.mark.integration


def test_reads_demo_dataset_from_mysql_via_superset_metadata() -> None:
    url = os.environ.get("WRITER_TEST_SOURCE_DB_URL")
    if not url:
        pytest.skip("WRITER_TEST_SOURCE_DB_URL not set")
    with SupersetClient(
        os.environ.get("WRITER_TEST_SUPERSET_URL", "http://127.0.0.1:8088"),
        os.environ.get("SUPERSET_ADMIN_USERNAME", "admin"),
        os.environ["SUPERSET_ADMIN_PASSWORD"],
    ) as superset:
        ds = superset.dataset(2)
    engine = create_engine(url)
    try:
        rows = list(DatasetReader(engine, page_size=15).rows(ds))
    finally:
        engine.dispose()
    assert ds.table_name == "assets"
    assert len(rows) == len(data.HARDWARE)
    retired = {a.asset_tag for a in data.HARDWARE if a.status == "retired"}
    assert retired and {r["asset_tag"] for r in rows if r["status"] == "retired"} == retired
