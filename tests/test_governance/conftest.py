from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from writer_service.governance.db import create_engine_for, make_session_factory
from writer_service.governance.migrate import upgrade


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'gov.sqlite'}"
    upgrade(url)  # schema comes from migrations, exactly like production
    eng = create_engine_for(url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s
