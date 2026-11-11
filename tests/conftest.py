import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start from a clean WRITER_* environment (CI sets some for the app)."""
    for key in list(os.environ):
        if key.startswith("WRITER_"):
            monkeypatch.delenv(key)
