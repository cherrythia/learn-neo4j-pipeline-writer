from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from demo_env.mock_superset import create_mock_superset
from demo_env.seed_sql import render_demo_inserts
from writer_service.api.deps import get_pipeline, get_reader, get_superset
from writer_service.app import create_app
from writer_service.config import Settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.superset import (
    DatasetMapping,
    DatasetReader,
    EntityMapping,
    SupersetClient,
    approve_mapping,
    save_draft,
)
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.pipeline.graph_store import InMemoryGraph
from writer_service.pipeline.orchestrator import WriterPipeline

KEY = "k" * 40


def source_engine() -> Engine:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def attach(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS demo_compliance")

    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        sql = render_demo_inserts().replace("CREATE TABLE ", "CREATE TABLE demo_compliance.")
        raw.executescript(sql.replace("INSERT INTO ", "INSERT INTO demo_compliance."))  # type: ignore[union-attr]
    return engine


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[TestClient, Any, InMemoryGraph]]:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    spec = OntologyService(engine, InMemoryGraphSchema()).bootstrap("admin")
    app = create_app(Settings(_env_file=None, governance_db_url=url, ingest_api_key=KEY))  # type: ignore[call-arg]
    graph = InMemoryGraph()
    client_superset = SupersetClient(
        "http://testserver", "admin", "pw", http=TestClient(create_mock_superset("admin", "pw"))
    )
    src = source_engine()
    app.dependency_overrides[get_pipeline] = lambda: WriterPipeline(engine, graph, None)
    app.dependency_overrides[get_superset] = lambda: client_superset
    app.dependency_overrides[get_reader] = lambda: DatasetReader(src, page_size=8)
    with TestClient(app) as c:
        yield c, (engine, client_superset, spec), graph
    src.dispose()


def approve_issuers(engine: Any, superset: SupersetClient) -> None:
    from writer_service.ontology.service import spec_of

    ds = superset.dataset(1)
    m = DatasetMapping(
        entities=[
            EntityMapping(
                class_name="Issuer",
                columns={
                    "ticker": "ticker",
                    "name": "name",
                    "board": "board",
                    "sector": "sector",
                    "country": "country",
                },
            )
        ]
    )
    with UnitOfWork(engine) as uow:
        draft = save_draft(uow, ds, m, suggested_by="user", actor="alice").id
        pub = uow.ontology_versions.published()
        assert pub is not None
        approve_mapping(uow, draft, spec_of(pub), ds, actor="alice")


def test_sync_requires_an_approved_mapping(setup: Any) -> None:
    client, _, _ = setup
    r = client.post("/ingest/superset/1", headers={"X-API-Key": KEY})
    assert r.status_code == 409 and "approved column mapping" in r.json()["detail"]


def test_sync_commits_all_issuers_deterministically(setup: Any) -> None:
    client, (engine, superset, _), graph = setup
    approve_issuers(engine, superset)
    r = client.post("/ingest/superset/1", headers={"X-API-Key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["counts"]["commit"] == 20
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["board"] == "mainboard"
    again = client.post("/ingest/superset/1", headers={"X-API-Key": KEY}).json()
    assert again["counts"]["commit"] == 20 and graph.node_count() == 40  # Issuer+Organisation


def test_sync_requires_api_key_and_known_dataset(setup: Any) -> None:
    client, (engine, superset, _), _ = setup
    assert client.post("/ingest/superset/1").status_code == 401
    approve_issuers(engine, superset)
    with UnitOfWork(engine) as uow:  # pretend dataset 999 has a mapping
        from writer_service.governance.models import ColumnMapping

        uow.session.add(
            ColumnMapping(
                dataset_id=999,
                dataset_name="x",
                status="approved",
                mapping={"entities": [], "relations": []},
                suggested_by="user",
            )
        )
    assert client.post("/ingest/superset/999", headers={"X-API-Key": KEY}).status_code == 404


def test_source_database_errors_are_502_without_internals(setup: Any) -> None:
    from sqlalchemy.exc import OperationalError

    client, (engine, superset, _), _ = setup
    approve_issuers(engine, superset)

    class Down(DatasetReader):
        def _rows(self, *_: Any, **__: Any) -> Any:
            raise OperationalError("SELECT secret_stuff", {}, Exception("conn refused"))

    down = Down(source_engine())
    client.app.dependency_overrides[get_reader] = lambda: down  # type: ignore[attr-defined]
    r = client.post("/ingest/superset/1", headers={"X-API-Key": KEY})
    assert r.status_code == 502 and "secret_stuff" not in r.text
