"""Claude adapter tests through the *real* anthropic SDK over a mocked HTTP transport.

Request building and response parsing are the SDK's own, so wire-format mistakes surface here.
"""

import json
from typing import Any

import anthropic
import httpx2
import pytest

from writer_service.llm.anthropic_client import FALLBACK_BETA, AnthropicLLM
from writer_service.llm.base import LLMOutputError, LLMRefusalError, LLMRequest

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
REQ = LLMRequest(system="sys", user="usr", schema=SCHEMA, prompt_name="p", prompt_version=1)


def usage(*iterations: dict[str, Any]) -> dict[str, Any]:
    u: dict[str, Any] = {"input_tokens": 10, "output_tokens": 5}
    if iterations:
        u["iterations"] = list(iterations)
    return u


def body(
    content: list[dict[str, Any]],
    stop: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    u: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": u or usage(),
    }


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


class Recorder:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=self.reply)

    @property
    def sent(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.requests[-1].content)
        return payload


def llm(reply: dict[str, Any], **kw: Any) -> tuple[AnthropicLLM, Recorder]:
    rec = Recorder(reply)
    sdk = anthropic.Anthropic(
        api_key="sk-test",
        http_client=httpx2.Client(transport=httpx2.MockTransport(rec)),
        max_retries=0,
    )
    return AnthropicLLM(sdk=sdk, model="claude-sonnet-5-5", **kw), rec


def test_request_on_the_wire_without_fallback() -> None:
    client, rec = llm(body([text('{"ok": true}')]), effort="medium", fallbacks=False)
    resp = client.complete_json(REQ)
    req = rec.requests[0]
    assert req.url.path == "/v1/messages"
    assert "anthropic-beta" not in req.headers
    sent = rec.sent
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    # Sonnet 5.5 rejects non-default sampling params and forced tool_choice.
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "tools", "fallbacks"):
        assert banned not in sent
    assert resp.data == {"ok": True}
    assert resp.model == "claude-sonnet-5-5"
    assert resp.usage == {"input_tokens": 10, "output_tokens": 5}


def test_fallback_is_requested_by_default() -> None:
    client, rec = llm(body([text('{"ok": true}')]))
    client.complete_json(REQ)
    assert FALLBACK_BETA in rec.requests[0].headers["anthropic-beta"]
    assert rec.sent["fallbacks"] == "default"


def test_fallback_turn_uses_only_the_serving_models_output() -> None:
    reply = body(
        [
            text('{"answ'),  # declining model's partial output
            {
                "type": "fallback",
                "from": {"model": "claude-sonnet-5-5"},
                "to": {"model": "claude-opus-5-5"},
                "trigger": {"type": "refusal"},
            },
            text('{"ok": true}'),
        ],
        u=usage(
            {
                "type": "message",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-opus-5-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        ),
    )
    client, _ = llm(reply)
    resp = client.complete_json(REQ)
    assert resp.data == {"ok": True}
    assert resp.model == "claude-opus-5-5"


def test_sticky_fallback_without_block_still_reports_serving_model() -> None:
    reply = body(
        [text('{"ok": false}')],
        u=usage(
            {
                "type": "fallback_message",
                "model": "claude-opus-5-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        ),
    )
    resp = llm(reply)[0].complete_json(REQ)
    assert resp.model == "claude-opus-5-5" and resp.data == {"ok": False}


@pytest.mark.parametrize("fallbacks", [True, False])
def test_refusal_raises_with_category(fallbacks: bool) -> None:
    reply = body([], stop="refusal")
    reply["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "declined"}
    client, _ = llm(reply, fallbacks=fallbacks)
    with pytest.raises(LLMRefusalError, match="category=cyber"):
        client.complete_json(REQ)


def test_truncated_output_raises() -> None:
    client, _ = llm(body([text('{"ok": tr')], stop="max_tokens"), fallbacks=False)
    with pytest.raises(LLMOutputError, match="max_tokens"):
        client.complete_json(REQ)


@pytest.mark.parametrize("payload", ["not json", "[1, 2]"])
def test_non_object_output_raises(payload: str) -> None:
    client, _ = llm(body([text(payload)]), fallbacks=False)
    with pytest.raises(LLMOutputError):
        client.complete_json(REQ)


def test_thinking_blocks_are_ignored() -> None:
    reply = body([{"type": "thinking", "thinking": "", "signature": "sig"}, text('{"ok": true}')])
    assert llm(reply, fallbacks=False)[0].complete_json(REQ).data == {"ok": True}
