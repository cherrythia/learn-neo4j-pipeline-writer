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
    assert "CREATE DATABASE IF NOT EXISTS demo_itam;" in sh
    assert "GRANT SELECT ON demo_itam.* TO 'reader'" in sh
    assert "GRANT ALL PRIVILEGES ON governance.* TO 'writer'" in sh
    assert "demo_itam.* TO 'writer'" not in sh


def test_no_passwords_hardcoded_in_init_scripts() -> None:
    sh = (ROOT / "infra/mysql/init/00_users.sh").read_text()
    assert "${MYSQL_WRITER_PASSWORD:?" in sh and "${MYSQL_READER_PASSWORD:?" in sh
    assert "IDENTIFIED BY" not in render_seed_sql()


def test_inserts_load_and_preserve_counts() -> None:
    # Inserts use portable SQL, so we can verify them on SQLite in CI.
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(render_demo_inserts())
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in data.TABLES}
    assert counts == {
        "departments": len(data.DEPARTMENTS),
        "assets": len(data.HARDWARE),
        "software": len(data.SOFTWARE),
        "applications": len(data.APPLICATIONS),
        "software_installs": len(data.SOFTWARE_INSTALLS),
    }


def test_costs_load_as_decimals() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(render_demo_inserts())
    tag = data.INVOICED_HARDWARE[3].asset_tag
    (cost,) = conn.execute("SELECT cost FROM assets WHERE asset_tag = ?", (tag,)).fetchone()
    assert cost == data.INVOICED_HARDWARE[3].cost
    assert "14800.00" in render_demo_inserts()


def test_quotes_in_text_are_escaped() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(render_demo_inserts())
    names = [r[0] for r in conn.execute("SELECT name FROM departments")]
    assert "CEO's Office" in names  # a name with an apostrophe survives the round-trip


def test_foreign_keys_declared_at_table_level() -> None:
    # MySQL ignores column-level REFERENCES, so only table-level FOREIGN KEY counts.
    sql = render_demo_inserts()
    assert "FOREIGN KEY (dept_code) REFERENCES departments(dept_code)" in sql
    assert "FOREIGN KEY (owner_dept) REFERENCES departments(dept_code)" in sql
    assert "FOREIGN KEY (asset_tag) REFERENCES assets(asset_tag)" in sql
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(render_demo_inserts())
    try:
        conn.execute("INSERT INTO software_installs VALUES (9999, 'SW-QDOC', 'HW-NOPE')")
    except sqlite3.IntegrityError:
        return
    raise AssertionError("install on an unknown asset accepted")
