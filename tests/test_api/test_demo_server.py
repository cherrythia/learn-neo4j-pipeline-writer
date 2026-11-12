"""The demo server factory: the real app over in-memory stores, seeded and ready to log in."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from writer_service.demo_server import ADMIN, REVIEWER, create_demo_app

CSRF = {"X-Requested-With": "writer-admin"}


@pytest.fixture
def demo(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Writer Admin</title>")
    app, _ = create_demo_app(
        tmp_path,
        admin_password="alice-password-123",
        reviewer_password="rita-password-123",
        admin_ui_dir=str(dist),
        ingest_api_key="demo-ingest-key-0123456789",
    )
    return TestClient(app)


def login(c: TestClient, user: str) -> None:
    r = c.post(
        "/api/auth/login",
        json={"username": user, "password": f"{user}-password-123"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text


def test_reviewer_logs_in_and_sees_seeded_review_items(demo: TestClient) -> None:
    login(demo, REVIEWER)
    assert demo.get("/api/auth/me").json() == {"username": REVIEWER, "role": "reviewer"}
    items = demo.get("/api/review").json()
    assert {i["reason"] for i in items} == {"score", "conflict"}
    assert len(items) == 3
    item = items[0]
    assert item["evidence"][0]["locator"].startswith("page ")
    assert set(item["breakdown"]["components"]) >= {"grounding", "back_translation"}
    # and works the queue end to end against the in-memory graph
    r = demo.post(f"/api/review/{item['id']}/approve", json={}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert len(demo.get("/api/review").json()) == 2


def test_admin_creates_and_publishes_a_draft(demo: TestClient) -> None:
    login(demo, ADMIN)
    r = demo.post("/api/ontology/drafts", headers=CSRF)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = demo.post(f"/api/ontology/versions/{version}/publish", headers=CSRF)
    assert r.status_code == 200, r.text


def test_admin_can_suggest_a_mapping_offline(demo: TestClient) -> None:
    login(demo, ADMIN)
    r = demo.post("/api/mappings/suggest/1", headers=CSRF)
    assert r.status_code == 201, r.text
    assert demo.get("/api/mappings").json()[0]["status"] == "draft"


def test_serves_the_admin_ui(demo: TestClient) -> None:
    assert "Writer Admin" in demo.get("/admin/").text


def test_generates_passwords_when_none_given(tmp_path: Path) -> None:
    _, state = create_demo_app(tmp_path)
    assert len(state.admin_password) >= 12 and len(state.reviewer_password) >= 12
    assert state.admin_password != state.reviewer_password


def test_document_upload_runs_offline_with_scripted_extraction(demo: TestClient) -> None:
    """Issue #17: the demo extracts the sample PDF without an API key (scripted answers)."""
    pdf = Path(__file__).resolve().parents[2] / "samples/aurora_pacific_annual_report_2025.pdf"
    r = demo.post(
        "/ingest/documents",
        files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
        headers={"X-API-Key": "demo-ingest-key-0123456789"},
    )
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "completed", run
    assert run["counts"]["review"] >= 10  # unsure back-translation: a human decides
    assert run["counts"]["reject"] == 0  # scripted answers are grounded in the PDF


def test_ingest_requires_the_demo_key(demo: TestClient) -> None:
    r = demo.post("/ingest/superset/1", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_close_deletes_the_temp_dir_it_created(tmp_path: Path) -> None:
    """Review round 1: the temp store holds password hashes; it must not outlive the demo."""
    _, state = create_demo_app()
    assert state.owns_workdir and (state.workdir / "governance.sqlite").exists()
    state.close()
    assert not state.workdir.exists()
    _, given = create_demo_app(tmp_path)  # a caller's directory is never deleted
    given.close()
    assert tmp_path.exists()
