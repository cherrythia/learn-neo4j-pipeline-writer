"""Candidate facts proposed by an extractor (LLM) or a mapping, before any verification.

A candidate is either an entity (a class instance identified by its identity key) or a
relation between two entities. Every candidate carries the evidence it came from.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from writer_service.ontology.spec import OntologySpec

SourceType = Literal["pdf", "excel", "superset"]
Scalar = str | int | float | bool | date | None


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    source_ref: str = Field(min_length=1)  # file path or dataset id
    locator: str = Field(min_length=1)  # "page 2", "sheet Checklist row 5", "row filing_id=X"
    quote: str = Field(min_length=1)  # the exact text/value the fact rests on


class EntityRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    class_name: str
    key: str  # identity key value


class CandidateEntity(BaseModel):
    kind: Literal["entity"] = "entity"
    class_name: str
    properties: dict[str, Scalar]
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class CandidateRelation(BaseModel):
    kind: Literal["relation"] = "relation"
    relation: str
    source: EntityRef
    target: EntityRef
    properties: dict[str, Scalar] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


Candidate = Annotated[CandidateEntity | CandidateRelation, Field(discriminator="kind")]


def canonical_text(value: str) -> str:
    """Trim and collapse internal whitespace (case is preserved for storage)."""
    return " ".join(value.split())


def match_key(value: Any) -> str:
    """Comparison form of a value: canonical text, case-folded; dates as ISO strings."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return canonical_text(str(value)).casefold()


def _digest(material: Any) -> str:
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def fact_hash(candidate: CandidateEntity | CandidateRelation) -> str:
    """Identity of the *fact*: what it states, not where it came from.

    Evidence and confidence are excluded, so the same fact from several runs or sources
    hashes identically (used for self-consistency voting and evidence merging).
    """
    props = {k: match_key(v) for k, v in candidate.properties.items() if v is not None}
    if isinstance(candidate, CandidateEntity):
        return _digest(["entity", candidate.class_name, props])
    return _digest(
        [
            "relation",
            candidate.relation,
            [candidate.source.class_name, match_key(candidate.source.key)],
            [candidate.target.class_name, match_key(candidate.target.key)],
            props,
        ]
    )


def identity_hash(candidate: CandidateEntity, spec: OntologySpec) -> str:
    """Identity of the *entity*: class + identity key value (properties ignored)."""
    cls = next(c for c in spec.classes if c.name == candidate.class_name)
    return _digest([candidate.class_name, match_key(candidate.properties.get(cls.identity_key))])
