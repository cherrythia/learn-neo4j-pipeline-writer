from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from writer_service.llm.base import (
    LLMOutputError,
    LLMRefusalError,
    LLMReplayMissError,
    LLMRequest,
    LLMResponse,
)
from writer_service.llm.cache import FileCache, InMemoryCache
from writer_service.llm.fake import FakeLLM, ReplayOnlyLLM
from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.schema import strict_json_schema
from writer_service.llm.structured import StructuredLLM


class Fact(BaseModel):
    subject: str
    confidence: float


class Facts(BaseModel):
    facts: list[Fact]


PROMPT = PromptTemplate(name="extract", version=3, system="Extract.", user="Text: {text}")
GOOD = {"facts": [{"subject": "Aurora", "confidence": 0.9}]}


def ask(llm: StructuredLLM, sample: int = 0, text: str = "t") -> Any:
    return llm.ask(PROMPT, {"text": text}, Facts, ontology_version=1, sample=sample)


def test_returns_validated_model() -> None:
    fake = FakeLLM({"extract": [GOOD]})
    result = ask(StructuredLLM(fake, InMemoryCache()))
    assert isinstance(result.value, Facts)
    assert result.value.facts[0].subject == "Aurora"
    assert result.cached is False and result.model == "fake"


def test_request_carries_strict_schema_and_metadata() -> None:
    fake = FakeLLM({"extract": [GOOD]})
    ask(StructuredLLM(fake, InMemoryCache()))
    (req,) = fake.calls
    assert req.prompt_name == "extract" and req.prompt_version == 3
    assert req.schema == strict_json_schema(Facts)
    assert req.user == "Text: t"


def test_cache_hit_returns_identical_output_without_calling_llm() -> None:
    fake = FakeLLM({"extract": [GOOD, {"facts": []}]})
    llm = StructuredLLM(fake, InMemoryCache())
    first, second = ask(llm), ask(llm)
    assert second.cached is True
    assert second.value == first.value
    assert len(fake.calls) == 1


def test_samples_have_distinct_cache_keys() -> None:
    """Self-consistency runs must really call the LLM N times, not replay sample 0."""
    fake = FakeLLM({"extract": [GOOD, {"facts": []}, GOOD]})
    llm = StructuredLLM(fake, InMemoryCache())
    results = [ask(llm, sample=i) for i in range(3)]
    assert len(fake.calls) == 3
    assert len({r.cache_key for r in results}) == 3
    assert results[1].value.facts == []


def test_cache_key_changes_with_prompt_version_ontology_and_input() -> None:
    llm = StructuredLLM(FakeLLM({"extract": [GOOD]}), InMemoryCache())
    base = llm.cache_key(PROMPT, {"text": "t"}, Facts, ontology_version=1, sample=0)
    bumped = PromptTemplate(name="extract", version=4, system="Extract.", user="Text: {text}")
    assert base != llm.cache_key(bumped, {"text": "t"}, Facts, ontology_version=1, sample=0)
    assert base != llm.cache_key(PROMPT, {"text": "t"}, Facts, ontology_version=2, sample=0)
    assert base != llm.cache_key(PROMPT, {"text": "u"}, Facts, ontology_version=1, sample=0)


def test_invalid_output_is_retried_with_feedback() -> None:
    fake = FakeLLM({"extract": [{"facts": [{"subject": "A"}]}, GOOD]})  # first misses confidence
    result = ask(StructuredLLM(fake, InMemoryCache(), max_attempts=2))
    assert result.value.facts[0].confidence == 0.9
    assert len(fake.calls) == 2
    assert "confidence" in fake.calls[1].user  # validation error fed back to the model


def test_gives_up_after_max_attempts_and_caches_nothing() -> None:
    cache = InMemoryCache()
    fake = FakeLLM({"extract": [{"nope": 1}]})
    with pytest.raises(LLMOutputError):
        ask(StructuredLLM(fake, cache, max_attempts=2))
    assert len(fake.calls) == 2
    assert cache.size() == 0


def test_replay_only_raises_on_cache_miss() -> None:
    llm = StructuredLLM(ReplayOnlyLLM("claude-sonnet-5-5"), InMemoryCache())
    with pytest.raises(LLMReplayMissError, match="no recorded response"):
        ask(llm)


def test_fake_supports_callables() -> None:
    def handler(req: LLMRequest) -> dict[str, Any]:
        return {"facts": [{"subject": req.user.upper(), "confidence": 1.0}]}

    result = ask(StructuredLLM(FakeLLM({"extract": handler}), InMemoryCache()), text="x")
    assert result.value.facts[0].subject == "TEXT: X"


def test_fake_unscripted_prompt_is_an_error() -> None:
    with pytest.raises(LLMOutputError, match="FakeLLM has no script"):
        FakeLLM({}).complete_json(
            LLMRequest(system="s", user="u", schema={}, prompt_name="other", prompt_version=1)
        )


def test_response_type_is_plain_data() -> None:
    r = LLMResponse(data={"a": 1}, model="m", stop_reason="end_turn")
    assert r.data == {"a": 1}


class Flaky:
    """Client that raises a scripted sequence of errors before answering."""

    def __init__(self, errors: list[Exception], answer: dict[str, Any] | None = GOOD) -> None:
        self.errors = errors
        self.answer = answer
        self.calls = 0

    @property
    def model(self) -> str:
        return "flaky"

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        assert self.answer is not None
        return LLMResponse(data=self.answer, model="flaky", stop_reason="end_turn")


def test_non_json_output_is_retried_then_succeeds() -> None:
    client = Flaky([LLMOutputError("response is not JSON")])
    result = ask(StructuredLLM(client, InMemoryCache(), max_attempts=2))
    assert client.calls == 2 and result.value.facts[0].subject == "Aurora"


def test_always_non_json_raises_and_caches_nothing() -> None:
    cache = InMemoryCache()
    client = Flaky([LLMOutputError("not JSON")] * 3, answer=None)
    with pytest.raises(LLMOutputError, match="invalid output after 3 attempts"):
        ask(StructuredLLM(client, cache, max_attempts=3))
    assert client.calls == 3 and cache.size() == 0


def test_refusal_is_not_retried() -> None:
    client = Flaky([LLMRefusalError("declined")])
    with pytest.raises(LLMRefusalError):
        ask(StructuredLLM(client, InMemoryCache(), max_attempts=3))
    assert client.calls == 1


def test_replay_miss_is_not_retried() -> None:
    client = Flaky([LLMReplayMissError("miss")])
    with pytest.raises(LLMReplayMissError):
        ask(StructuredLLM(client, InMemoryCache(), max_attempts=3))
    assert client.calls == 1


def test_record_then_replay_from_file_cache(tmp_path: Path) -> None:
    """A FileCache recorded with a real model replays offline under the same model name."""
    model = "claude-sonnet-5-5"
    recorder = FakeLLM({"extract": [GOOD]}, model=model)  # stands in for the live provider
    recorded = ask(StructuredLLM(recorder, FileCache(tmp_path)))
    replayed = ask(StructuredLLM(ReplayOnlyLLM(model), FileCache(tmp_path)))
    assert replayed.cached is True and replayed.value == recorded.value
    assert replayed.model == model
    with pytest.raises(LLMReplayMissError):  # a different model is a different key
        ask(StructuredLLM(ReplayOnlyLLM("claude-opus-5-5"), FileCache(tmp_path)))
