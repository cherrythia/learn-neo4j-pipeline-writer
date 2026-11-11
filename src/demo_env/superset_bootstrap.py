"""Register the demo MySQL database and datasets in a *real* Superset (compose profile).

Runs as a one-shot container after Superset is healthy. Uses only the standard library.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from demo_env.data import DATASETS, SCHEMA


def database_payload(host: str, password: str) -> dict[str, Any]:
    return {
        "database_name": SCHEMA,
        "sqlalchemy_uri": f"mysql+pymysql://reader:{password}@{host}:3306/{SCHEMA}",
        "expose_in_sqllab": True,
    }


def dataset_payloads(database_id: int) -> list[dict[str, Any]]:
    return [
        {"database": database_id, "schema": SCHEMA, "table_name": d.table_name} for d in DATASETS
    ]


class _Client:
    """JSON client that keeps cookies: Superset binds the CSRF token to the session cookie."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=self.headers
        )
        try:
            with self._opener.open(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            exc.msg = f"{exc.msg}: {method} {path}: {detail}"
            raise


def main() -> None:
    base = os.environ.get("SUPERSET_URL", "http://superset:8088")
    client = _Client(base)
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/health", timeout=10).close()
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(5)
    else:
        raise SystemExit(f"Superset at {base} did not become healthy")
    token = client.call(
        "POST",
        "/api/v1/security/login",
        {
            "username": os.environ["SUPERSET_ADMIN_USERNAME"],
            "password": os.environ["SUPERSET_ADMIN_PASSWORD"],
            "provider": "db",
            "refresh": False,
        },
    )["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    csrf = client.call("GET", "/api/v1/security/csrf_token/")["result"]
    client.headers["X-CSRFToken"] = csrf

    existing = client.call("GET", "/api/v1/database/")["result"]
    db = next((d for d in existing if d["database_name"] == SCHEMA), None)
    if db is None:
        db_payload = database_payload("mysql", os.environ["MYSQL_READER_PASSWORD"])
        db_id = client.call("POST", "/api/v1/database/", db_payload)["id"]
    else:
        db_id = db["id"]
    for payload in dataset_payloads(db_id):
        try:
            client.call("POST", "/api/v1/dataset/", payload)
            print(f"registered dataset {payload['table_name']}")
        except urllib.error.HTTPError as exc:
            if exc.code != 422:  # 422 = already exists
                raise
    print("superset bootstrap complete")


if __name__ == "__main__":
    main()
