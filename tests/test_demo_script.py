"""Issue #17: ``scripts/demo.sh`` runs the whole story end to end against the offline demo
server (in-memory stores, scripted LLM). CI runs this, so the README walkthrough can't rot."""

import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_demo_script_runs_end_to_end() -> None:
    env = {**os.environ, "WRITER_DEMO_PORT": str(free_port())}
    env.pop("WRITER_ADMIN_UI_DIR", None)
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/demo.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    for step in (
        "1. upload",
        "2. review queue",
        "3. approve",
        "4. superset mapping",
        "5. superset sync",
        "6. rollback",
        "7. audit",
    ):
        assert step in out, out
    assert "demo complete" in out
