"""Run governance-store migrations programmatically or from the CLI.

uv run writer-db upgrade            # to head, using WRITER_GOVERNANCE_DB_URL
uv run writer-db downgrade base
uv run writer-db revision -m "add x" # autogenerate a new migration
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def main(argv: list[str] | None = None) -> None:
    from writer_service.config import get_settings

    parser = argparse.ArgumentParser(prog="writer-db")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upgrade").add_argument("revision", nargs="?", default="head")
    sub.add_parser("downgrade").add_argument("revision")
    rev = sub.add_parser("revision")
    rev.add_argument("-m", "--message", required=True)
    args = parser.parse_args(argv)

    url = get_settings().governance_db_url
    if args.cmd == "upgrade":
        upgrade(url, args.revision)
    elif args.cmd == "downgrade":
        downgrade(url, args.revision)
    else:
        command.revision(alembic_config(url), message=args.message, autogenerate=True)


if __name__ == "__main__":
    main()
