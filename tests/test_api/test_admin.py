"""Admin API: a permission matrix over every route, plus each workflow end to end."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.pool import StaticPool

from demo_env.mock_superset import create_mock_superset
from demo_env.seed_sql import render_demo_inserts
from writer_service.api.deps import get_graph_schema, get_pipeline, get_reader, get_superset
from writer_service.app import create_app
from writer_service.auth.users import create_user
from writer_service.config import Settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.models import AuditLog, OntologyVersion
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.superset import MAPPING_PROMPT, DatasetReader, SupersetClient
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.pipeline.candidates import CandidateEntity, Evidence
from writer_service.pipeline.graph_store import InMemoryGraph
from writer_service.pipeline.orchestrator import SourceBatch, SourceText, WriterPipeline
from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

CSRF = {"X-Requested-With": "writer-admin"}


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


class Env:
    def __init__(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'g.sqlite'}"
        upgrade(url)
        self.engine = create_engine_for(url)
        self.schema = InMemoryGraphSchema()
        OntologyService(self.engine, self.schema).bootstrap("setup")
        with UnitOfWork(self.engine) as uow:
            create_user(uow, "alice", "alice-password-123", "admin", actor="setup")
            create_user(uow, "rita", "rita-password-123", "reviewer", actor="setup")
        self.graph = InMemoryGraph()
        self.fake = FakeLLM(
            {
                BACK_TRANSLATION_PROMPT.name: [{"agrees": True, "score": 0.5, "explanation": "?"}],
                MAPPING_PROMPT.name: [
                    {
                        "entities": [
                            {
                                "class_name": "Issuer",
                                "columns": [
                                    {"property": "ticker", "column": "ticker"},
                                    {"property": "name", "column": "name"},
                                ],
                            }
                        ],
                        "relations": [],
                    }
                ],
            }
        )
        self.pipeline = WriterPipeline(
            self.engine, self.graph, StructuredLLM(self.fake, InMemoryCache())
        )
        self.app = create_app(Settings(_env_file=None, governance_db_url=url))  # type: ignore[call-arg]
        self.superset = SupersetClient(
            "http://testserver", "admin", "pw", http=TestClient(create_mock_superset("admin", "pw"))
        )
        self.source = source_engine()
        self.app.dependency_overrides[get_pipeline] = lambda: self.pipeline
        self.app.dependency_overrides[get_graph_schema] = lambda: self.schema
        self.app.dependency_overrides[get_superset] = lambda: self.superset
        self.app.dependency_overrides[get_reader] = lambda: DatasetReader(self.source)

    def client(self, user: str | None = None) -> TestClient:
        c = TestClient(self.app)
        if user:
            r = c.post(
                "/api/auth/login",
                json={"username": user, "password": f"{user}-password-123"},
                headers=CSRF,
            )
            assert r.status_code == 200, r.text
        return c

    def review_item(self) -> int:
        """Ingest a document fact that lands in review (judge score 0.5)."""
        e = Evidence(source_type="pdf", source_ref="a.pdf", locator="page 1", quote="APH Co")
        cand = CandidateEntity(
            class_name="Issuer", properties={"ticker": "APH", "name": "APH Co"}, evidence=[e]
        )
        batch = SourceBatch(
            "pdf",
            "a.pdf",
            [SourceText("a.pdf", "page 1", "APH Co is listed")],
            extract=lambda s: [cand],
        )
        self.pipeline.ingest(batch, actor="bot")
        with UnitOfWork(self.engine) as uow:
            from writer_service.governance.models import ReviewItem

            return uow.session.scalars(select(ReviewItem)).one().id


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.source.dispose()


# --------------------------------------------------------------------------- permission matrix

ADMIN_ONLY_PREFIXES = (
    "/api/ontology/drafts",
    "/api/ontology/rollback",
    "/api/mappings",
    "/api/audit",
    "/api/users",
)
ADMIN_ONLY_SUFFIXES = ("/publish", "/approve", "/reject", "/rollback", "/classes", "/relations")


def admin_routes(app: Any) -> list[tuple[str, str, str]]:
    """(method, path, required role) for every /api route except auth, from the OpenAPI schema
    (included routers aren't flattened in ``app.routes`` in this FastAPI version)."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            continue
        for method in ops:
            m = method.upper()
            reviewer_ok = (
                m == "GET" and not path.startswith(("/api/mappings", "/api/audit", "/api/users"))
            ) or (m, path) in {
                ("POST", "/api/proposals"),
                ("POST", "/api/review/{item_id}/approve"),
                ("POST", "/api/review/{item_id}/reject"),
            }
            out.append((m, path, "reviewer" if reviewer_ok else "admin"))
    return out


def concrete(path: str) -> str:
    return (
        path.replace("{version}", "1")
        .replace("{name}", "Issuer")
        .replace("{item_id}", "1")
        .replace("{proposal_id}", "1")
        .replace("{mapping_id}", "1")
        .replace("{dataset_id}", "1")
        .replace("{run_id}", "x")
        .replace("{username}", "rita")
    )


def test_matrix_covers_all_routes(env: Env) -> None:
    routes = admin_routes(env.app)
    assert len(routes) >= 25
    assert ("POST", "/api/ontology/versions/{version}/publish", "admin") in routes
    assert ("POST", "/api/review/{item_id}/approve", "reviewer") in routes


def test_every_route_requires_login(env: Env) -> None:
    c = env.client()
    routes = admin_routes(env.app)
    assert len(routes) >= 25  # guard against an empty (vacuously passing) matrix
    for method, path, _ in routes:
        r = c.request(method, concrete(path), headers=CSRF, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_reviewer_is_refused_on_every_admin_route(env: Env) -> None:
    c = env.client("rita")
    admin_only = [r for r in admin_routes(env.app) if r[2] == "admin"]
    assert len(admin_only) >= 18
    for method, path, _ in admin_only:
        r = c.request(method, concrete(path), headers=CSRF, json={})
        assert r.status_code == 403, (method, path, r.status_code)


def test_reviewer_can_reach_reviewer_routes(env: Env) -> None:
    c = env.client("rita")
    reviewer_routes = [r for r in admin_routes(env.app) if r[2] == "reviewer"]
    assert len(reviewer_routes) >= 8
    for method, path, _ in reviewer_routes:
        r = c.request(method, concrete(path), headers=CSRF, json={})
        assert r.status_code not in (401, 403), (method, path, r.status_code)


def test_writes_need_the_csrf_header(env: Env) -> None:
    c = env.client("alice")
    assert c.post("/api/ontology/drafts").status_code == 403
    assert c.post("/api/ontology/drafts", headers=CSRF).status_code == 201


# --------------------------------------------------------------------------- ontology


def test_reviewer_cannot_publish_or_roll_back_and_nothing_changes(env: Env) -> None:
    admin, rita = env.client("alice"), env.client("rita")
    assert admin.post("/api/ontology/drafts", headers=CSRF).json() == {"version": 2}
    before = list(env.schema.executed)
    assert rita.post("/api/ontology/versions/2/publish", headers=CSRF).status_code == 403
    assert rita.post("/api/ontology/rollback", headers=CSRF).status_code == 403
    with UnitOfWork(env.engine) as uow:
        assert {v.version: v.status for v in uow.session.scalars(select(OntologyVersion))} == {
            1: "published",
            2: "draft",
        }
    assert env.schema.executed == before


def test_draft_edit_publish_and_rollback(env: Env) -> None:
    c = env.client("alice")
    c.post("/api/ontology/drafts", headers=CSRF)
    r = c.post(
        "/api/ontology/versions/2/classes",
        headers=CSRF,
        json={
            "name": "ESGMetric",
            "identity_key": "metric_id",
            "properties": [{"name": "metric_id", "required": True}, {"name": "unit"}],
        },
    )
    assert r.status_code == 201
    assert (
        c.post(
            "/api/ontology/versions/2/relations",
            headers=CSRF,
            json={"name": "REPORTS", "domain": "Filing", "range": "ESGMetric"},
        ).status_code
        == 201
    )
    assert (
        c.patch(
            "/api/ontology/versions/2/classes/Finding",
            headers=CSRF,
            json={"add_properties": [{"name": "status"}]},
        ).status_code
        == 200
    )
    assert c.post("/api/ontology/versions/2/publish", headers=CSRF).json() == {"published": 2}
    assert "wsc_ext_esgmetric_metric_id" in env.schema.names
    detail = c.get("/api/ontology/versions/2").json()
    assert detail["status"] == "published"
    assert any(cl["name"] == "ESGMetric" and cl["namespace"] == "ext" for cl in detail["classes"])
    assert c.post("/api/ontology/rollback", headers=CSRF).json() == {"published": 1}


def test_publish_gate_errors_are_422_and_published_versions_are_read_only(env: Env) -> None:
    c = env.client("alice")
    c.post("/api/ontology/drafts", headers=CSRF)
    c.patch("/api/ontology/versions/2/classes/Issuer", headers=CSRF, json={"deprecated": True})
    r = c.post("/api/ontology/versions/2/publish", headers=CSRF)
    assert r.status_code == 422 and "deprecated" in r.json()["detail"]
    assert (
        c.post(
            "/api/ontology/versions/1/classes",
            headers=CSRF,
            json={"name": "X", "identity_key": "id", "properties": [{"name": "id"}]},
        ).status_code
        == 409
    )


# --------------------------------------------------------------------------- proposals


def test_proposal_flow_adds_to_a_draft(env: Env) -> None:
    rita, alice = env.client("rita"), env.client("alice")
    pid = rita.post(
        "/api/proposals",
        headers=CSRF,
        json={
            "kind": "class",
            "rationale": "ESG reports mention emissions",
            "payload": {
                "name": "Emission",
                "identity_key": "emission_id",
                "properties": [{"name": "emission_id", "required": True}],
            },
        },
    ).json()["id"]
    assert rita.get("/api/proposals?status=pending").json()[0]["id"] == pid
    assert alice.post(f"/api/proposals/{pid}/approve", headers=CSRF).json() == {"draft": 2}
    classes = alice.get("/api/ontology/versions/2").json()["classes"]
    assert any(c["name"] == "Emission" and c["namespace"] == "ext" for c in classes)
    assert alice.post(f"/api/proposals/{pid}/approve", headers=CSRF).status_code == 409


def test_invalid_proposal_payload_is_422(env: Env) -> None:
    r = env.client("rita").post(
        "/api/proposals", headers=CSRF, json={"kind": "class", "payload": {"name": "X"}}
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- review


def test_review_queue_list_approve(env: Env) -> None:
    item = env.review_item()
    c = env.client("rita")
    (row,) = c.get("/api/review").json()
    assert row["id"] == item and row["reason"] == "score"
    assert row["breakdown"]["components"]["back_translation"]["value"] == 0.5
    assert row["evidence"][0]["quote"] == "APH Co"
    assert c.post(f"/api/review/{item}/approve", headers=CSRF, json={}).json() == {
        "status": "approved"
    }
    assert env.graph.node_properties("Issuer", "ticker", "APH") is not None
    assert c.post(f"/api/review/{item}/approve", headers=CSRF, json={}).status_code == 409


def test_review_reject_with_note(env: Env) -> None:
    item = env.review_item()
    c = env.client("rita")
    assert (
        c.post(f"/api/review/{item}/reject", headers=CSRF, json={"note": "weak"}).status_code == 200
    )
    assert c.get("/api/review").json() == []
    assert c.get("/api/review?status=rejected").json()[0]["note"] == "weak"


# --------------------------------------------------------------------------- mappings


def test_mapping_suggest_and_approve(env: Env) -> None:
    c = env.client("alice")
    r = c.post("/api/mappings/suggest/1", headers=CSRF)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    assert r.json()["mapping"]["entities"][0]["columns"] == {"ticker": "ticker", "name": "name"}
    assert c.post(f"/api/mappings/{mid}/approve", headers=CSRF).json() == {"status": "approved"}
    (m,) = c.get("/api/mappings").json()
    assert m["status"] == "approved" and m["approved_by"] == "alice"


def test_invalid_manual_mapping_cannot_be_approved(env: Env) -> None:
    c = env.client("alice")
    mid = c.post(
        "/api/mappings",
        headers=CSRF,
        json={
            "dataset_id": 1,
            "mapping": {"entities": [{"class_name": "Issuer", "columns": {"name": "name"}}]},
        },
    ).json()["id"]
    r = c.post(f"/api/mappings/{mid}/approve", headers=CSRF)
    assert r.status_code == 422 and "identity key 'ticker'" in r.json()["detail"]


# --------------------------------------------------------------------------- scoring, runs, users


def test_scoring_get_put_and_validation(env: Env) -> None:
    alice = env.client("alice")
    policy = alice.get("/api/scoring").json()
    policy["commit_threshold"] = 0.9
    assert alice.put("/api/scoring", headers=CSRF, json=policy).json()["commit_threshold"] == 0.9
    policy["review_threshold"] = 0.95
    assert alice.put("/api/scoring", headers=CSRF, json=policy).status_code == 422
    assert env.client("rita").get("/api/scoring").json()["commit_threshold"] == 0.9


def test_runs_list_detail_and_rollback(env: Env) -> None:
    env.review_item()
    c = env.client("alice")
    (run,) = c.get("/api/runs").json()
    detail = c.get(f"/api/runs/{run['id']}").json()
    assert detail["candidates"][0]["decision"] == "review"
    r = c.post(f"/api/runs/{run['id']}/rollback", headers=CSRF).json()
    assert r["review_items_closed"] == 1
    assert c.post(f"/api/runs/{run['id']}/rollback", headers=CSRF).status_code == 409


def test_audit_lists_newest_first(env: Env) -> None:
    c = env.client("alice")
    c.post("/api/ontology/drafts", headers=CSRF)
    entries = c.get("/api/audit?limit=5").json()
    assert entries[0]["action"] == "ontology.draft" and entries[0]["actor"] == "alice"


def test_users_create_patch_and_self_protection(env: Env) -> None:
    c = env.client("alice")
    r = c.post(
        "/api/users",
        headers=CSRF,
        json={"username": "sam", "password": "sam-password-1234", "role": "reviewer"},
    )
    assert r.status_code == 201 and r.json()["role"] == "reviewer"
    assert (
        c.post(
            "/api/users",
            headers=CSRF,
            json={"username": "sam", "password": "sam-password-1234", "role": "admin"},
        ).status_code
        == 409
    )
    rita = env.client("rita")
    assert (
        c.patch("/api/users/rita", headers=CSRF, json={"active": False}).json()["active"] is False
    )
    assert rita.get("/api/review").status_code == 401  # session ended immediately
    assert c.patch("/api/users/alice", headers=CSRF, json={"active": False}).status_code == 409
    with UnitOfWork(env.engine) as uow:
        assert "user.update" in [a.action for a in uow.session.scalars(select(AuditLog))]
