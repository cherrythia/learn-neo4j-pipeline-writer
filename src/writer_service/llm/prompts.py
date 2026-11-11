"""Versioned prompt templates. Bump ``version`` whenever wording changes: it is part of the
cache key, so old cached answers are never reused for a new prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: int
    system: str
    user: str

    def render(self, **variables: Any) -> tuple[str, str]:
        """Fill ``{placeholders}``; a missing variable raises ``KeyError``."""
        return self.system.format_map(variables), self.user.format_map(variables)
