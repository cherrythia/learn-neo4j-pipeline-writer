"""Manage admin users.

uv run writer-users create alice --role admin     # prompts for the password
"""

from __future__ import annotations

import argparse
import getpass

from writer_service.auth.users import create_user
from writer_service.config import get_settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.uow import UnitOfWork


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="writer-users")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create")
    create.add_argument("username")
    create.add_argument("--role", choices=["admin", "reviewer"], required=True)
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        raise SystemExit("error: passwords do not match")
    engine = create_engine_for(get_settings().governance_db_url)
    try:
        with UnitOfWork(engine) as uow:
            create_user(uow, args.username, password, args.role, actor=getpass.getuser())
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(f"created {args.role} {args.username}")


if __name__ == "__main__":
    main()
