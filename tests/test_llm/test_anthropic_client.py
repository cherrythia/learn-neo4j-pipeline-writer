from types import SimpleNamespace
from typing import Any

import pytest

from writer_service.llm.anthropic_client import AnthropicLLM
from writer_service.llm.base import LLMOutputError, LLMRefusalError, LLMRequest

REQ = LLMRequest(
    system="sys",
    user="usr",
    schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    prompt_name="p",
    prompt_version=1,
)


def message(
    text: str = '{"ok": true}', stop: str = "end_turn", model: str = "claude-sonnet-5-5"
) -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        stop_details=None,
        model=model,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class StubSDK:
    """Stands in for anthropic.Anthropic; records the kwargs we send."""

    def __init__(self, reply: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))
        self.reply = reply

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.reply


def test_sends_structured_output_request_without_sampling_params() -> None:
    sdk = StubSDK(message())
    resp = AnthropicLLM(
        sdk=sdk, model="claude-sonnet-5-5", effort="high", fallbacks=False
    ).complete_json(REQ)
    (kw,) = sdk.calls
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": "usr"}]
    assert kw["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": REQ.schema},
    }
    # Sonnet 5.5 rejects non-default sampling params and forced tool_choice.
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "betas", "fallbacks"):
        assert banned not in kw
    assert resp.data == {"ok": True}
    assert resp.usage == {"input_tokens": 10, "output_tokens": 5}


def test_server_side_fallback_enabled_by_default() -> None:
    sdk = StubSDK(message(model="claude-opus-5-5"))
    resp = AnthropicLLM(sdk=sdk, model="claude-sonnet-5-5").complete_json(REQ)
    (kw,) = sdk.calls
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert resp.model == "claude-opus-5-5"  # records which model actually served


def test_refusal_raises() -> None:
    sdk = StubSDK(message(text="", stop="refusal"))
    with pytest.raises(LLMRefusalError):
        AnthropicLLM(sdk=sdk, model="m", fallbacks=False).complete_json(REQ)


def test_truncated_output_raises() -> None:
    sdk = StubSDK(message(text='{"ok": tr', stop="max_tokens"))
    with pytest.raises(LLMOutputError, match="max_tokens"):
        AnthropicLLM(sdk=sdk, model="m", fallbacks=False).complete_json(REQ)


def test_non_json_text_raises() -> None:
    sdk = StubSDK(message(text="not json"))
    with pytest.raises(LLMOutputError):
        AnthropicLLM(sdk=sdk, model="m", fallbacks=False).complete_json(REQ)
