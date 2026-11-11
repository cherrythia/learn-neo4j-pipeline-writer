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
