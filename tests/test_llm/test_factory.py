import pytest

from writer_service.config import Settings
from writer_service.llm.anthropic_client import AnthropicLLM
from writer_service.llm.base import LLMConfigError
from writer_service.llm.factory import build_llm_client
from writer_service.llm.fake import FakeLLM


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_fake_provider() -> None:
    assert isinstance(build_llm_client(settings(llm_provider="fake")), FakeLLM)


def test_anthropic_without_key_fails_fast() -> None:
    with pytest.raises(LLMConfigError, match="WRITER_ANTHROPIC_API_KEY"):
        build_llm_client(settings(llm_provider="anthropic"))


def test_anthropic_with_key_uses_configured_model_and_effort() -> None:
    client = build_llm_client(
        settings(llm_provider="anthropic", anthropic_api_key="sk-test", llm_effort="medium")
    )
    assert isinstance(client, AnthropicLLM)
    assert client.model == "claude-sonnet-5-5" and client.effort == "medium"
