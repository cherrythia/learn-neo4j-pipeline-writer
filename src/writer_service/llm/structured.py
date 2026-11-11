"""Validated, cached structured output on top of any ``LLMClient``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from writer_service.llm.base import LLMClient, LLMOutputError, LLMRequest
from writer_service.llm.cache import LLMCache
from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.schema import strict_json_schema


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    value: T
    model: str
    cached: bool
    cache_key: str


class StructuredLLM:
    def __init__(self, client: LLMClient, cache: LLMCache, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.client = client
        self.cache = cache
        self.max_attempts = max_attempts

    def cache_key(
        self,
        prompt: PromptTemplate,
        variables: dict[str, Any],
        output: type[BaseModel],
        *,
        ontology_version: int,
        sample: int,
    ) -> str:
        """Hash of everything that can change the answer.

        ``sample`` distinguishes self-consistency runs, so N runs make N real calls the first
        time and replay N independent answers afterwards.
        """
        system, user = prompt.render(**variables)
        material = {
            "model": self.client.model,
            "prompt": [prompt.name, prompt.version],
            "system": system,
            "user": user,
            "schema": strict_json_schema(output),
            "ontology_version": ontology_version,
            "sample": sample,
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def ask[T: BaseModel](
        self,
        prompt: PromptTemplate,
        variables: dict[str, Any],
        output: type[T],
        *,
        ontology_version: int,
        sample: int = 0,
    ) -> LLMResult[T]:
        key = self.cache_key(
            prompt, variables, output, ontology_version=ontology_version, sample=sample
        )
        hit = self.cache.get(key)
        if hit is not None:
            return LLMResult(output.model_validate(hit["data"]), hit["model"], True, key)

        system, user = prompt.render(**variables)
        schema = strict_json_schema(output)
        feedback = ""
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            response = self.client.complete_json(
                LLMRequest(
                    system=system,
                    user=user + feedback,
                    schema=schema,
                    prompt_name=prompt.name,
                    prompt_version=prompt.version,
                )
            )
            try:
                value = output.model_validate(response.data)
            except ValidationError as exc:
                last_error = exc
                feedback = (
                    "\n\nYour previous answer did not match the required schema:\n"
                    f"{exc}\nReturn corrected JSON only."
                )
                continue
            # Only validated answers are cached; failures are never replayed.
            self.cache.put(key, {"data": value.model_dump(mode="json"), "model": response.model})
            return LLMResult(value, response.model, False, key)
        raise LLMOutputError(
            f"{prompt.name} v{prompt.version}: invalid output after "
            f"{self.max_attempts} attempts: {last_error}"
        )
