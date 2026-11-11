import sqlite3
from pathlib import Path

from demo_env import data
from demo_env.seed_sql import SEED_PATH, render_demo_inserts, render_seed_sql

ROOT = Path(__file__).resolve().parents[2]


def test_committed_seed_file_is_up_to_date() -> None:
    committed = (ROOT / SEED_PATH).read_text()
    assert committed == render_seed_sql(), "run: uv run python -m demo_env.seed_sql"


def test_users_script_grants_least_privilege() -> None:
    sh = (ROOT / "infra/mysql/init/00_users.sh").read_text()
    assert "GRANT SELECT ON demo_compliance.* TO 'reader'" in sh
    assert "GRANT ALL PRIVILEGES ON governance.* TO 'writer'" in sh
    assert "demo_compliance.* TO 'writer'" not in sh


def test_no_passwords_hardcoded_in_init_scripts() -> None:
    sh = (ROOT / "infra/mysql/init/00_users.sh").read_text()
    assert "${MYSQL_WRITER_PASSWORD:?" in sh and "${MYSQL_READER_PASSWORD:?" in sh
    assert "IDENTIFIED BY" not in render_seed_sql()


def test_inserts_load_and_preserve_counts() -> None:
    # Inserts use portable SQL, so we can verify them on SQLite in CI.
    conn = sqlite3.connect(":memory:")
    conn.executescript(render_demo_inserts())
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("issuers", "filings", "rules", "obligations")
    }
    assert counts == {
        "issuers": len(data.ISSUERS),
        "filings": len(data.FILINGS),
        "rules": len(data.RULES),
        "obligations": len(data.OBLIGATIONS),
    }


def test_quotes_in_text_are_escaped() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(render_demo_inserts())
    texts = [r[0] for r in conn.execute("SELECT text FROM rules")]
    assert any("'" in t for t in texts)  # a rule with an apostrophe survives round-trip
