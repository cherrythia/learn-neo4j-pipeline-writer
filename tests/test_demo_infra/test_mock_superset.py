import pytest
from fastapi.testclient import TestClient

from demo_env import data
from demo_env.mock_superset import DATASETS, create_mock_superset

USER, PASSWORD = "admin", "admin"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_mock_superset(username=USER, password=PASSWORD))


def login(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/v1/security/login",
        json={"username": USER, "password": PASSWORD, "provider": "db", "refresh": False},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_login_rejects_bad_password(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/security/login",
        json={"username": USER, "password": "nope", "provider": "db"},
    )
    assert resp.status_code == 401


def test_dataset_requires_token(client: TestClient) -> None:
    assert client.get("/api/v1/dataset/1").status_code == 401
    bad = {"Authorization": "Bearer forged"}
    assert client.get("/api/v1/dataset/1", headers=bad).status_code == 401


def test_list_datasets(client: TestClient) -> None:
    body = client.get("/api/v1/dataset/", headers=login(client)).json()
    assert body["count"] == len(DATASETS) == 4
    names = {d["table_name"] for d in body["result"]}
    assert names == {"issuers", "filings", "rules", "obligations"}


def test_get_dataset_shape_matches_superset(client: TestClient) -> None:
    body = client.get("/api/v1/dataset/1", headers=login(client)).json()
    result = body["result"]
    assert result["id"] == 1
    assert result["table_name"] == "issuers"
    assert result["schema"] == "demo_compliance"
    assert result["database"]["backend"] == "mysql"
    assert result["sql"] is None
    cols = {c["column_name"] for c in result["columns"]}
    assert {"issuer_id", "name", "board"} <= cols


def test_dataset_columns_match_demo_rows(client: TestClient) -> None:
    headers = login(client)
    for ds in DATASETS:
        res = client.get(f"/api/v1/dataset/{ds.id}", headers=headers).json()["result"]
        cols = [c["column_name"] for c in res["columns"]]
        assert cols == list(data.columns_of(ds.table_name))


def test_unknown_dataset_404(client: TestClient) -> None:
    assert client.get("/api/v1/dataset/999", headers=login(client)).status_code == 404
