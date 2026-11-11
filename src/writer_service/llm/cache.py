"""Caches for validated LLM results, keyed by a content hash (see ``StructuredLLM``).

A ``FileCache`` directory doubles as a set of **recorded fixtures**: commit it and replay it in
CI with ``ReplayOnlyLLM`` so tests are deterministic and never call a real model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

_KEY = re.compile(r"^[0-9a-f]{8,128}$")


class LLMCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, entry: dict[str, Any]) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._store.get(key)

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self._store[key] = entry

    def size(self) -> int:
        return len(self._store)


class FileCache:
    """One pretty-printed JSON file per key, so recordings are reviewable in diffs."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError(f"invalid cache key: {key!r}")
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        entry: dict[str, Any] = json.loads(path.read_text())
        return entry

    def put(self, key: str, entry: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)  # atomic: readers never see a half-written file
