"""A tiny stand-in for the Apache Superset REST API.

Implements only the endpoints the writer service's Superset connector uses:
``POST /api/v1/security/login``, ``GET /api/v1/dataset/`` and ``GET /api/v1/dataset/{id}``.
Response shapes follow Superset's API so the connector works unchanged against real Superset.

Run:  ``uv run uvicorn demo_env.mock_superset:create_mock_superset --factory --port 8088``
"""

from __future__ import annotations

import os
import secrets
from datetime import date
from typing import Any, get_type_hints

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from demo_env.data import DATASETS, SCHEMA, TABLES, Dataset, columns_of

__all__ = ["DATASETS", "create_mock_superset"]
_DATABASE = {"id": 1, "database_name": SCHEMA, "backend": "mysql"}


def _sql_type(py_type: Any) -> str:
    if py_type is int:
        return "INTEGER"
    if py_type is float:
        return "DECIMAL(10, 2)"
    if py_type is date:
        return "DATE"
    return "VARCHAR"


def _dataset_body(ds: Dataset) -> dict[str, Any]:
    hints = get_type_hints(TABLES[ds.table_name])
    return {
        "id": ds.id,
        "table_name": ds.table_name,
        "schema": SCHEMA,
        "sql": None,
        "kind": "physical",
        "database": _DATABASE,
        "columns": [
            {"column_name": c, "type": _sql_type(hints[c])} for c in columns_of(ds.table_name)
        ],
    }


class LoginRequest(BaseModel):
    username: str
    password: str
    provider: str = "db"
    refresh: bool = False


def create_mock_superset(username: str | None = None, password: str | None = None) -> FastAPI:
    user = username or os.environ.get("MOCK_SUPERSET_USERNAME", "admin")
    pw = password or os.environ.get("MOCK_SUPERSET_PASSWORD", "admin")
    tokens: set[str] = set()
    app = FastAPI(title="Mock Superset API")

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing bearer token")
        if authorization.removeprefix("Bearer ") not in tokens:
            raise HTTPException(401, "Invalid token")

    @app.post("/api/v1/security/login")
    def login(body: LoginRequest) -> dict[str, str]:
        user_ok = secrets.compare_digest(body.username, user)
        pw_ok = secrets.compare_digest(body.password, pw)
        if not (user_ok and pw_ok):
            raise HTTPException(401, "Invalid login")
        token = secrets.token_urlsafe(24)
        tokens.add(token)
        return {"access_token": token}

    @app.get("/api/v1/dataset/", dependencies=[Depends(require_token)])
    def list_datasets() -> dict[str, Any]:
        return {"count": len(DATASETS), "result": [_dataset_body(d) for d in DATASETS]}

    @app.get("/api/v1/dataset/{dataset_id}", dependencies=[Depends(require_token)])
    def get_dataset(dataset_id: int) -> dict[str, Any]:
        for ds in DATASETS:
            if ds.id == dataset_id:
                return {"id": ds.id, "result": _dataset_body(ds)}
        raise HTTPException(404, "Not found")

    return app
