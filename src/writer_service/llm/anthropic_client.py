"""Anthropic (Claude) adapter.

Uses structured outputs (``output_config.format`` with a JSON schema) rather than forced tool
use, which Claude Sonnet 5.5 rejects. Sampling parameters (temperature/top_p) are not sent:
Sonnet 5.5 rejects non-default values, so run-to-run variation is handled by caching and
self-consistency instead (ADR-0005).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from writer_service.llm.base import LLMOutputError, LLMRefusalError, LLMRequest, LLMResponse

Effort = Literal["low", "medium", "high", "xhigh", "max"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(
        self,
        *,
        sdk: Any,
        model: str,
        effort: Effort = "high",
        fallbacks: bool = True,
    ) -> None:
        """``sdk`` is an ``anthropic.Anthropic`` client (injected so tests can stub it)."""
        self._sdk = sdk
        self._model = model
        self.effort = effort
        self.fallbacks = fallbacks

    @property
    def model(self) -> str:
        return self._model

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        }
        if self.fallbacks:
            # On a safety refusal the API re-runs the request on a suitable model.
            message = self._sdk.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            message = self._sdk.messages.create(**kwargs)

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise LLMRefusalError(f"{request.prompt_name}: model refused (category={category})")
        if message.stop_reason == "max_tokens":
            raise LLMOutputError(f"{request.prompt_name}: output truncated at max_tokens")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMOutputError(f"{request.prompt_name}: response is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMOutputError(f"{request.prompt_name}: expected a JSON object")
        usage = {
            "input_tokens": int(getattr(message.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(message.usage, "output_tokens", 0) or 0),
        }
        return LLMResponse(
            data=data, model=message.model, stop_reason=message.stop_reason, usage=usage
        )
