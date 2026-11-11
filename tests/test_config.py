import pytest
from pydantic import ValidationError

from writer_service.config import Settings


def make(**overrides: object) -> Settings:
    # _env_file=None keeps tests independent of a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_are_safe_for_local_dev() -> None:
    s = make()
    assert s.llm_provider == "anthropic"
    assert s.llm_model == "claude-sonnet-5-5"
    assert s.self_consistency_runs == 3
    assert s.anthropic_api_key is None


def test_self_consistency_runs_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make(self_consistency_runs=0)


def test_unknown_llm_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        make(llm_provider="not-a-provider")


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITER_SELF_CONSISTENCY_RUNS", "1")
    monkeypatch.setenv("WRITER_LLM_PROVIDER", "fake")
    s = make()
    assert s.self_consistency_runs == 1
    assert s.llm_provider == "fake"


def test_secrets_are_not_leaked_in_repr() -> None:
    s = make(anthropic_api_key="sk-secret-value", neo4j_password="pw-secret")
    assert "sk-secret-value" not in repr(s)
    assert "pw-secret" not in repr(s)
