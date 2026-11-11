"""Deterministic stand-ins for a real LLM, used in tests and CI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from writer_service.llm.base import LLMOutputError, LLMReplayMissError, LLMRequest, LLMResponse

Script = Sequence[dict[str, Any]] | Callable[[LLMRequest], dict[str, Any]]


class FakeLLM:
    """Answers by prompt name.

    A list script returns its items in order and then keeps repeating the last one; a callable
    script computes the answer from the request. Every request is recorded in ``calls``.
    """

    def __init__(self, scripts: Mapping[str, Script] | None = None) -> None:
        self._scripts = dict(scripts or {})
        self._positions: dict[str, int] = {}
        self.calls: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "fake"

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        script = self._scripts.get(request.prompt_name)
        if script is None:
            raise LLMOutputError(f"FakeLLM has no script for prompt {request.prompt_name!r}")
        if callable(script):
            data = script(request)
        else:
            i = self._positions.get(request.prompt_name, 0)
            data = script[min(i, len(script) - 1)]
            self._positions[request.prompt_name] = i + 1
        return LLMResponse(data=data, model=self.model, stop_reason="end_turn")


class ReplayOnlyLLM:
    """Used with a recorded ``FileCache``: any cache miss is an error, never a live call."""

    @property
    def model(self) -> str:
        return "replay"

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        raise LLMReplayMissError(
            f"no recorded response for prompt {request.prompt_name!r} "
            f"v{request.prompt_version}; record it with a real provider first"
        )
