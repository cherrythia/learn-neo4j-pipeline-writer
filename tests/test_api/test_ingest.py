from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from demo_env.samples import PDF_PATH, XLSX_PATH
from writer_service.api.deps import get_pipeline
from writer_service.app import create_app
from writer_service.config import Settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.ingestion.documents import EXTRACTION_PROMPT
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.pipeline.graph_store import InMemoryGraph
from writer_service.pipeline.orchestrator import WriterPipeline
from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

ROOT = Path(__file__).resolve().parents[2]
KEY = "k" * 40
HEADERS = {"X-API-Key": KEY}

EXTRACTED = {
    "entities": [
        {
            "class_name": "Vendor",
            "properties": [
                {"name": "name", "value": "Zorblax Systems Supply (Demo)"},
                {"name": "country", "value": "Singapore"},
            ],
            "evidence": [
                {
                    "locator": "page 1",
                    "quote": "Zorblax Systems Supply (Demo), 1 Example Way, Demo City 000000",
                }
            ],
            "confidence": 0.95,
        }
    ],
    "relations": [],
}


def make_client(tmp_path: Path, *, bootstrap: bool = True, **settings: object) -> TestClient:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    if bootstrap:
        OntologyService(engine, InMemoryGraphSchema()).bootstrap("admin")
    s = Settings(
        _env_file=None,
        governance_db_url=url,
        ingest_api_key=KEY,  # type: ignore[call-arg]
        **settings,
    )
    app = create_app(s)
    fake = FakeLLM(
        {
            EXTRACTION_PROMPT.name: [EXTRACTED],
            BACK_TRANSLATION_PROMPT.name: [{"agrees": True, "score": 0.95, "explanation": "ok"}],
        }
    )
    graph = InMemoryGraph()
    pipe = WriterPipeline(engine, graph, StructuredLLM(fake, InMemoryCache()))
    app.dependency_overrides[get_pipeline] = lambda: pipe
    app.state.test_graph = graph
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as c:
        yield c


def upload(client: TestClient, path: Path, name: str | None = None, headers: dict | None = None):  # type: ignore[no-untyped-def]
    with path.open("rb") as fh:
        return client.post(
            "/ingest/documents",
            files={"file": (name or path.name, fh)},
            headers=HEADERS if headers is None else headers,
        )


def test_pdf_upload_runs_the_pipeline(client: TestClient) -> None:
    r = upload(client, ROOT / PDF_PATH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["counts"]["commit"] == 1
    graph = client.app.state.test_graph  # type: ignore[attr-defined]
    assert graph.node_properties("Vendor", "name", "Zorblax Systems Supply (Demo)") is not None


def test_excel_upload_is_accepted(client: TestClient) -> None:
    r = upload(client, ROOT / XLSX_PATH)
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_requires_api_key(client: TestClient) -> None:
    assert upload(client, ROOT / PDF_PATH, headers={}).status_code == 401


def test_unsupported_type(client: TestClient, tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    assert upload(client, f).status_code == 415


def test_unreadable_pdf(client: TestClient, tmp_path: Path) -> None:
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"not a pdf at all")
    r = upload(client, f)
    assert r.status_code == 422 and "not a readable PDF" in r.json()["detail"]


def test_client_path_is_stripped(client: TestClient) -> None:
    r = upload(client, ROOT / PDF_PATH, name="../../etc/invoice.pdf")
    assert r.status_code == 200
    graph = client.app.state.test_graph  # type: ignore[attr-defined]
    assert {v["source"] for v in graph.evidence.values()} == {"invoice.pdf"}


def test_too_large(tmp_path: Path) -> None:
    with make_client(tmp_path, max_upload_mb=1) as c:
        big = tmp_path / "big.pdf"
        big.write_bytes(b"%PDF" + b"0" * (1024 * 1024 + 10))
        assert upload(c, big).status_code == 413


def test_no_published_ontology(tmp_path: Path) -> None:
    with make_client(tmp_path, bootstrap=False) as c:
        assert upload(c, ROOT / PDF_PATH).status_code == 409


def test_llm_not_configured_is_503(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    s = Settings(_env_file=None, governance_db_url=url, ingest_api_key=KEY)  # type: ignore[call-arg]
    with TestClient(create_app(s)) as c:  # real get_pipeline: anthropic provider, no key
        r = upload(c, ROOT / PDF_PATH)
    assert r.status_code == 503 and "WRITER_ANTHROPIC_API_KEY" in r.json()["detail"]


def test_failed_run_does_not_leak_internal_errors(tmp_path: Path) -> None:
    with make_client(tmp_path) as c:
        c.app.state.test_graph.fail_next_write = True  # type: ignore[attr-defined]
        body = upload(c, ROOT / PDF_PATH).json()
    assert body["status"] == "failed"
    assert body["error"] == f"run failed; details are stored with run {body['run_id']}"


def test_neo4j_driver_closed_on_shutdown(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    app = create_app(Settings(_env_file=None, governance_db_url=url))  # type: ignore[call-arg]
    driver = MagicMock()
    with TestClient(app):
        app.state.neo4j_driver = driver
    driver.close.assert_called_once()


def test_pipeline_is_built_once(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    app = create_app(Settings(_env_file=None, governance_db_url=url))  # type: ignore[call-arg]
    sentinel = object()
    app.state.pipeline = sentinel
    request = MagicMock(app=app)
    assert get_pipeline(request) is sentinel
