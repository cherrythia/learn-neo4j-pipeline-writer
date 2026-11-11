from typing import Any

import pytest
from pydantic import BaseModel

from writer_service.llm.base import LLMOutputError, LLMRequest, LLMResponse
from writer_service.llm.cache import InMemoryCache
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
    llm = StructuredLLM(ReplayOnlyLLM(), InMemoryCache())
    with pytest.raises(LLMOutputError, match="no recorded response"):
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
