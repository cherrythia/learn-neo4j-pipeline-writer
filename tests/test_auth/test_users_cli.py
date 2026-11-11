from pathlib import Path

import pytest
from sqlalchemy import select

from writer_service.auth.cli import main
from writer_service.auth.passwords import verify_password
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.models import User
from writer_service.governance.uow import UnitOfWork


def test_create_user_via_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    monkeypatch.setenv("WRITER_GOVERNANCE_DB_URL", url)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-long-password-1")
    main(["create", "bob", "--role", "reviewer"])
    with UnitOfWork(create_engine_for(url)) as uow:
        bob = uow.session.scalars(select(User).where(User.username == "bob")).one()
        assert bob.role == "reviewer" and verify_password("a-long-password-1", bob.password_hash)
    with pytest.raises(SystemExit, match="already exists"):
        main(["create", "bob", "--role", "admin"])
