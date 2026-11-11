from pathlib import Path

import pytest
from pydantic import ValidationError

from writer_service.config import Settings


def make(**overrides: object) -> Settings:
    # _env_file=None keeps tests independent of a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_are_safe_for_local_dev() -> None:
    s = make()
    assert s.app_env == "dev"
    assert s.llm_provider == "anthropic"
    assert s.llm_model == "claude-sonnet-5-5"
    assert s.self_consistency_runs == 3
    assert s.anthropic_api_key is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"self_consistency_runs": 0},
        {"self_consistency_runs": 11},
        {"llm_provider": "not-a-provider"},
        {"app_env": "staging"},
    ],
)
def test_invalid_values_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)


@pytest.mark.parametrize("runs", [1, 10])
def test_self_consistency_runs_bounds_accepted(runs: int) -> None:
    assert make(self_consistency_runs=runs).self_consistency_runs == runs


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITER_SELF_CONSISTENCY_RUNS", "1")
    monkeypatch.setenv("WRITER_LLM_PROVIDER", "fake")
    s = make()
    assert s.self_consistency_runs == 1
    assert s.llm_provider == "fake"


def test_empty_api_key_in_env_file_is_treated_as_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("WRITER_ANTHROPIC_API_KEY=\n")
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.anthropic_api_key is None


def test_env_example_loads_cleanly() -> None:
    s = Settings(_env_file=Path(__file__).parent.parent / ".env.example")  # type: ignore[call-arg]
    assert s.anthropic_api_key is None
    assert s.self_consistency_runs == 3


def test_secrets_are_not_leaked_in_repr() -> None:
    s = make(anthropic_api_key="sk-secret-value", neo4j_password="pw-secret")
    assert "sk-secret-value" not in repr(s)
    assert "pw-secret" not in repr(s)


def test_prod_requires_strong_jwt_secret_and_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="WRITER_ADMIN_JWT_SECRET"):
        make(app_env="prod", cookie_secure=True)
    with pytest.raises(ValidationError, match="at least 32"):
        make(app_env="prod", cookie_secure=True, admin_jwt_secret="short")
    with pytest.raises(ValidationError, match="WRITER_COOKIE_SECURE"):
        make(app_env="prod", admin_jwt_secret="x" * 32)
    ok = make(app_env="prod", cookie_secure=True, admin_jwt_secret="x" * 32)
    assert ok.app_env == "prod"
    assert "x" * 32 not in repr(ok)


@pytest.mark.parametrize("key", ["short", "dev-ingest-key-change-me"])
def test_prod_rejects_weak_ingest_key(key: str) -> None:
    with pytest.raises(ValidationError, match="WRITER_INGEST_API_KEY"):
        make(app_env="prod", cookie_secure=True, admin_jwt_secret="x" * 32, ingest_api_key=key)
