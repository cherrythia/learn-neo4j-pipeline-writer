"""Build the configured LLM stack from settings."""

from __future__ import annotations

from writer_service.config import Settings
from writer_service.llm.anthropic_client import AnthropicLLM
from writer_service.llm.base import LLMClient, LLMConfigError
from writer_service.llm.cache import FileCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        return FakeLLM()
    if settings.anthropic_api_key is None:
        raise LLMConfigError(
            "WRITER_LLM_PROVIDER=anthropic needs WRITER_ANTHROPIC_API_KEY "
            "(or set WRITER_LLM_PROVIDER=fake for offline development)"
        )
    import anthropic

    sdk = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return AnthropicLLM(
        sdk=sdk,
        model=settings.llm_model,
        effort=settings.llm_effort,
        fallbacks=settings.llm_fallbacks,
    )


def build_structured_llm(settings: Settings) -> StructuredLLM:
    return StructuredLLM(
        build_llm_client(settings),
        FileCache(settings.llm_cache_dir),
        max_attempts=settings.llm_max_attempts,
    )
