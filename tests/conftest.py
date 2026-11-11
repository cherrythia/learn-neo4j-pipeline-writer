import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start from a clean WRITER_* environment (CI sets some for the app).

    ``WRITER_TEST_*`` variables configure the test run itself and are kept.
    """
    for key in list(os.environ):
        if key.startswith("WRITER_") and not key.startswith("WRITER_TEST_"):
            monkeypatch.delenv(key)
