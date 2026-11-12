"""A self-contained demo of the writer service with the admin UI (no Docker, no API key).

    uv run writer-demo-server            # http://127.0.0.1:8000/admin/

The real FastAPI app, with:
- a temporary SQLite governance store (migrated, core ontology published),
- an in-memory graph and graph schema instead of Neo4j,
- a scripted ``FakeLLM`` instead of Claude (document extraction replays the golden-set stand-in
  answers for the sample vendor invoice, so uploading that PDF works offline),
- the mock Superset API and the demo CMDB rows (so mappings can be suggested and approved),
- users ``alice`` (admin) and ``rita`` (reviewer),
- a few ingested candidates waiting in the review queue (one of them a conflict).

Passwords come from ``WRITER_DEMO_ADMIN_PASSWORD`` / ``WRITER_DEMO_REVIEWER_PASSWORD`` and the
ingestion key from ``WRITER_DEMO_INGEST_API_KEY``, or they are generated and printed on
startup. Everything is thrown away when the process exits.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from demo_env.mock_superset import create_mock_superset
from demo_env.seed_sql import render_demo_inserts
from writer_service.api.deps import get_graph_schema, get_pipeline, get_reader, get_superset
from writer_service.app import create_app
from writer_service.auth.users import create_user
from writer_service.config import Settings
from writer_service.evaluation.scripted import RUNS as SCRIPTED_EXTRACTION
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.documents import EXTRACTION_PROMPT
from writer_service.ingestion.superset import MAPPING_PROMPT, DatasetReader, SupersetClient
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.pipeline.candidates import CandidateEntity, Evidence
from writer_service.pipeline.graph_store import InMemoryGraph
from writer_service.pipeline.orchestrator import (
    AnyCandidate,
    SourceBatch,
    SourceText,
    WriterPipeline,
)
from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

ADMIN, REVIEWER = "alice", "rita"
_SUPERSET_USER, _SUPERSET_PASSWORD = "admin", "demo-superset"

# Back-translation agrees but is unsure: document facts land in review rather than commit.
_LLM_SCRIPTS: dict[str, Any] = {
    BACK_TRANSLATION_PROMPT.name: [
        {"agrees": True, "score": 0.5, "explanation": "plausible, not certain"}
    ],
    # Three self-consistency runs: one invents an asset, one misses one, voting repairs both.
    EXTRACTION_PROMPT.name: SCRIPTED_EXTRACTION,
    MAPPING_PROMPT.name: [
        {
            "entities": [
                {
                    "class_name": "Department",
                    "columns": [
                        {"property": "dept_code", "column": "dept_code"},
                        {"property": "name", "column": "name"},
                    ],
                }
            ],
            "relations": [],
        }
    ],
}


@dataclass
class DemoState:
    """What the demo app is made of (handy in tests)."""

    workdir: Path
    engine: Engine
    graph: InMemoryGraph
    schema: InMemoryGraphSchema
    pipeline: WriterPipeline
    admin_password: str
    reviewer_password: str
    ingest_api_key: str
    owns_workdir: bool = False

    def close(self) -> None:
        """Dispose the store; delete the work dir if the demo created it (it holds the demo
        users' password hashes and the audit log)."""
        self.engine.dispose()
        if self.owns_workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


def _source_engine() -> Engine:
    """The demo CMDB rows in an in-memory SQLite, as the dataset reader expects."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def attach(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS demo_itam")

    with engine.begin() as conn:
        raw: Any = conn.connection.dbapi_connection
        sql = render_demo_inserts().replace("CREATE TABLE ", "CREATE TABLE demo_itam.")
        raw.executescript(sql.replace("INSERT INTO ", "INSERT INTO demo_itam."))
    return engine


def _department(code: str, name: str, source_type: Any, ref: str, locator: str) -> CandidateEntity:
    return CandidateEntity(
        class_name="Department",
        properties={"dept_code": code, "name": name},
        evidence=[Evidence(source_type=source_type, source_ref=ref, locator=locator, quote=name)],
    )


def seed(pipeline: WriterPipeline) -> None:
    """One committed fact, then document facts that need a human: two by score, one conflict."""
    committed = _department("ITS", "IT Services", "superset", "dataset 1", "row dept_code=ITS")
    pipeline.ingest(
        SourceBatch(
            "superset",
            "dataset 1",
            [SourceText("dataset 1", "row dept_code=ITS", "ITS IT Services")],
            candidates=[committed],
        ),
        actor="demo",
    )
    ref = "purchase-order.pdf"
    pages = [
        ("page 1", _department("FAC", "Facilities", "pdf", ref, "page 1")),
        ("page 2", _department("PRC", "Procurement", "pdf", ref, "page 2")),
        # same code, different name than the committed fact: a conflict
        ("page 3", _department("ITS", "Information Technology", "pdf", ref, "page 3")),
    ]
    for locator, cand in pages:
        quote = cand.evidence[0].quote
        pipeline.ingest(
            SourceBatch(
                "pdf",
                ref,
                [SourceText(ref, locator, f"Deliver to the {quote} department")],
                extract=_always(cand),
            ),
            actor="demo",
        )


def _always(cand: CandidateEntity) -> Callable[[int], Sequence[AnyCandidate]]:
    """A stand-in extractor that finds the same candidate on every self-consistency run."""
    return lambda _sample: [cand]


def create_demo_app(
    workdir: Path | None = None,
    *,
    admin_password: str | None = None,
    reviewer_password: str | None = None,
    admin_ui_dir: str | None = None,
    ingest_api_key: str | None = None,
) -> tuple[FastAPI, DemoState]:
    """Build the demo app. ``workdir`` holds the SQLite file (a new temp dir by default)."""
    owns_workdir = workdir is None
    workdir = workdir or Path(tempfile.mkdtemp(prefix="writer-demo-"))
    url = f"sqlite:///{workdir / 'governance.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    schema = InMemoryGraphSchema()
    OntologyService(engine, schema).bootstrap("demo")
    admin_password = admin_password or secrets.token_urlsafe(12)
    reviewer_password = reviewer_password or secrets.token_urlsafe(12)
    ingest_api_key = ingest_api_key or secrets.token_urlsafe(32)
    with UnitOfWork(engine) as uow:
        create_user(uow, ADMIN, admin_password, "admin", actor="demo")
        create_user(uow, REVIEWER, reviewer_password, "reviewer", actor="demo")

    graph = InMemoryGraph()
    pipeline = WriterPipeline(engine, graph, StructuredLLM(FakeLLM(_LLM_SCRIPTS), InMemoryCache()))
    seed(pipeline)

    settings = Settings(
        _env_file=None,
        governance_db_url=url,
        llm_provider="fake",
        ingest_api_key=ingest_api_key,
        admin_ui_dir=admin_ui_dir,
    )
    app = create_app(settings)
    superset = SupersetClient(
        "http://mock-superset",
        _SUPERSET_USER,
        _SUPERSET_PASSWORD,
        http=TestClient(create_mock_superset(_SUPERSET_USER, _SUPERSET_PASSWORD)),
    )
    reader = DatasetReader(_source_engine())
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    app.dependency_overrides[get_graph_schema] = lambda: schema
    app.dependency_overrides[get_superset] = lambda: superset
    app.dependency_overrides[get_reader] = lambda: reader
    state = DemoState(
        workdir,
        engine,
        graph,
        schema,
        pipeline,
        admin_password,
        reviewer_password,
        ingest_api_key,
        owns_workdir=owns_workdir,
    )
    return app, state


def main() -> None:
    import uvicorn

    host = os.environ.get("WRITER_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("WRITER_DEMO_PORT", "8000"))
    app, state = create_demo_app(
        admin_password=os.environ.get("WRITER_DEMO_ADMIN_PASSWORD"),
        reviewer_password=os.environ.get("WRITER_DEMO_REVIEWER_PASSWORD"),
        admin_ui_dir=os.environ.get("WRITER_ADMIN_UI_DIR") or None,
        ingest_api_key=os.environ.get("WRITER_DEMO_INGEST_API_KEY"),
    )
    print(
        f"\nwriter demo: http://{host}:{port}/admin/  (API docs: /docs)\n"
        f"  {ADMIN} (admin)    password: {state.admin_password}\n"
        f"  {REVIEWER} (reviewer) password: {state.reviewer_password}\n"
        f"  X-API-Key for /ingest/*:  {state.ingest_api_key}\n"
        f"  governance store: {state.workdir / 'governance.sqlite'} (temporary)\n",
        flush=True,
    )
    # uvicorn shuts down gracefully on SIGTERM and then re-raises the signal; with the default
    # handler that would kill the process before the cleanup below. Exit normally instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        state.close()


if __name__ == "__main__":
    main()
