"""Export the OpenAPI schema for the admin UI's typed client.

uv run writer-openapi            # writes admin-ui/src/api/openapi.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from writer_service.app import create_app
from writer_service.config import Settings

OUTPUT = Path("admin-ui/src/api/openapi.json")


def schema() -> dict[str, Any]:
    app = create_app(Settings(_env_file=None))
    spec: dict[str, Any] = app.openapi()
    return spec


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
