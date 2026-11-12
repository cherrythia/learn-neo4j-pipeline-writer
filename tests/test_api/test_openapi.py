from pathlib import Path

from writer_service.api.openapi_export import OUTPUT, render

ROOT = Path(__file__).resolve().parents[2]


def test_committed_openapi_is_up_to_date() -> None:
    """The admin UI's typed client is generated from this file; regenerate with
    `uv run writer-openapi` after changing any route or model."""
    assert (ROOT / OUTPUT).read_text() == render(), "run: uv run writer-openapi"


def test_schema_documents_the_admin_api() -> None:
    import json

    paths = json.loads(render())["paths"]
    for p in (
        "/api/review",
        "/api/ontology/versions/{version}/publish",
        "/api/scoring",
        "/ingest/documents",
        "/api/auth/login",
    ):
        assert p in paths
