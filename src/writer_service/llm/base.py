"""The provider-neutral contract every LLM adapter implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMConfigError(LLMError):
    """The LLM cannot be used with the current configuration (e.g. missing API key)."""


class LLMOutputError(LLMError):
    """The model returned something unusable (invalid JSON, schema mismatch, truncated)."""


class LLMRefusalError(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    schema: dict[str, Any]  # JSON schema the output must satisfy
    prompt_name: str
    prompt_version: int
    max_tokens: int = 16000


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]
    model: str  # the model that actually served the request (may differ after fallback)
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        """Return JSON matching ``request.schema``; raise an :class:`LLMError` otherwise."""
        ...
