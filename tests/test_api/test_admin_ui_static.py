"""The built admin UI is served at /admin/ (only when it has been built)."""

from pathlib import Path

from fastapi.testclient import TestClient

from writer_service.app import create_app
from writer_service.config import Settings

INDEX = "<!doctype html><title>Writer Admin</title><div id=root></div>"


def app_with_dist(tmp_path: Path, dist: Path) -> TestClient:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        governance_db_url=f"sqlite:///{tmp_path / 'g.sqlite'}",
        admin_ui_dir=str(dist),
    )
    return TestClient(create_app(settings))


def test_admin_serves_index_and_assets(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    (dist / "index-abc.js").write_text("console.log('ui')")
    client = app_with_dist(tmp_path, dist)

    r = client.get("/admin/")
    assert r.status_code == 200
    assert r.text == INDEX
    assert r.headers["content-type"].startswith("text/html")
    assert client.get("/admin/index-abc.js").text == "console.log('ui')"
    # Without the slash, relative asset URLs would break: redirect to /admin/.
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers["location"].endswith("/admin/")
    assert client.get("/admin/missing.js").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}


def test_no_mount_without_a_build(tmp_path: Path) -> None:
    client = app_with_dist(tmp_path, tmp_path / "not-built")
    assert client.get("/admin/").status_code == 404


def test_ui_mount_is_not_in_the_api_schema(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    paths = app_with_dist(tmp_path, dist).app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/admin") for p in paths)
