"""Optional live check against the real Claude API. Costs a few cents; never runs in CI.

WRITER_TEST_ANTHROPIC_API_KEY=sk-... uv run pytest -m live
"""

import os

import pytest
from pydantic import BaseModel

from writer_service.config import Settings
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.factory import build_llm_client
from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.structured import StructuredLLM

pytestmark = pytest.mark.live

KEY = os.environ.get("WRITER_TEST_ANTHROPIC_API_KEY")


class Capital(BaseModel):
    country: str
    capital: str


def test_structured_output_roundtrip() -> None:
    if not KEY:
        pytest.skip("WRITER_TEST_ANTHROPIC_API_KEY not set")
    settings = Settings(_env_file=None, anthropic_api_key=KEY, llm_effort="low")  # type: ignore[call-arg]
    llm = StructuredLLM(build_llm_client(settings), InMemoryCache())
    prompt = PromptTemplate("capital", 1, "Answer with the requested JSON.", "Capital of {c}?")
    result = llm.ask(prompt, {"c": "France"}, Capital, ontology_version=0)
    assert result.value.capital.lower() == "paris"
