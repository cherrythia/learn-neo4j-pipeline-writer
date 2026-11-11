"""Verification signals for scoring (ADR-0005): evidence grounding, back-translation and
self-consistency. Each turns "the LLM said so" into a number the scoring engine can weigh.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.structured import StructuredLLM
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    Evidence,
    canonical_text,
    fact_hash,
)

AnyCandidate = CandidateEntity | CandidateRelation

# ------------------------------------------------------------------------ grounding


def _norm(text: str) -> str:
    return canonical_text(text).casefold()


def grounding_score(quote: str, source_text: str | None) -> float:
    """1.0 if the quote appears verbatim (ignoring whitespace and case); otherwise the best
    fuzzy partial match in [0, 0.99]. No source text means nothing can be verified: 0."""
    if not source_text or not quote.strip():
        return 0.0
    q, s = _norm(quote), _norm(source_text)
    if q in s:
        return 1.0
    return min(0.99, fuzz.partial_ratio(q, s) / 100.0)


class DictSourceText:
    """Source text per (source_ref, locator), e.g. one entry per PDF page or Excel row."""

    def __init__(self, texts: Mapping[tuple[str, str], str]) -> None:
        self.texts = dict(texts)

    def text_for(self, evidence: Evidence) -> str | None:
        return self.texts.get((evidence.source_ref, evidence.locator))

    def grounding(self, candidate: AnyCandidate) -> float:
        """Best grounding over the candidate's evidence (0 without evidence)."""
        return max(
            (grounding_score(e.quote, self.text_for(e)) for e in candidate.evidence),
            default=0.0,
        )


# ------------------------------------------------------------------------ back-translation


def describe(candidate: AnyCandidate) -> str:
    """The fact in plain language: what the judge checks against the evidence."""
    if isinstance(candidate, CandidateRelation):
        s, t = candidate.source, candidate.target
        return f"{s.class_name} '{s.key}' {candidate.relation} {t.class_name} '{t.key}'"
    parts = [f"{k} '{v}'" for k, v in candidate.properties.items() if v is not None]
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"{candidate.class_name} with {joined}" if parts else candidate.class_name


BACK_TRANSLATION_PROMPT = PromptTemplate(
    name="back_translation",
    version=1,
    system=(
        "You verify extracted facts against source quotes for a compliance knowledge graph. "
        "Judge only from the quotes given; do not use outside knowledge. A fact is supported "
        "only if the quotes state it (paraphrase is fine; inference beyond the text is not)."
    ),
    user=(
        "Fact: {fact}\n\nSource quotes:\n{quotes}\n\n"
        "Restate the fact in your own words, compare it with the quotes, and answer with: "
        "agrees (true/false), score (0 to 1: how fully the quotes support the fact), and a "
        "one-sentence explanation."
    ),
)
DISAGREE_CAP = 0.3  # a judge that disagrees can't contribute more than this


class Judgement(BaseModel):
    agrees: bool
    score: float = Field(ge=0, le=1)
    explanation: str


class BackTranslator:
    def __init__(self, llm: StructuredLLM, ontology_version: int) -> None:
        self.llm = llm
        self.ontology_version = ontology_version

    def score(self, candidate: AnyCandidate) -> float:
        if not candidate.evidence:
            return 0.0
        quotes = "\n".join(f"- [{e.locator}] {e.quote}" for e in candidate.evidence)
        result = self.llm.ask(
            BACK_TRANSLATION_PROMPT,
            {"fact": describe(candidate), "quotes": quotes},
            Judgement,
            ontology_version=self.ontology_version,
        )
        j = result.value
        return j.score if j.agrees else min(j.score, DISAGREE_CAP)


# ------------------------------------------------------------------------ self-consistency


@dataclass(frozen=True)
class VotedCandidate:
    candidate: AnyCandidate  # evidence merged across runs, confidence averaged
    votes: int
    stability: float  # votes / n


def self_consistency(
    extract: Callable[[int], Sequence[AnyCandidate]], n: int
) -> list[VotedCandidate]:
    """Run ``extract(sample)`` for sample 0..n-1 and keep facts a strict majority agrees on.

    Facts are compared by ``fact_hash`` (case/whitespace/order-insensitive). A fact repeated
    within one run counts once. Output order is first appearance, so it is deterministic.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    groups: dict[str, list[AnyCandidate]] = {}
    for sample in range(n):
        seen: set[str] = set()
        for cand in extract(sample):
            h = fact_hash(cand)
            if h in seen:
                continue
            seen.add(h)
            groups.setdefault(h, []).append(cand)
    voted: list[VotedCandidate] = []
    for members in groups.values():
        if len(members) * 2 <= n:  # strict majority: 2 of 3, 1 of 1, 2 of 2
            continue
        evidence: list[Evidence] = []
        for m in members:
            for e in m.evidence:
                if e not in evidence:
                    evidence.append(e)
        merged = members[0].model_copy(
            update={
                "evidence": evidence,
                "confidence": sum(m.confidence for m in members) / len(members),
            }
        )
        voted.append(VotedCandidate(merged, len(members), len(members) / n))
    return voted
