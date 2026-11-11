"""Verification signals for scoring (ADR-0005): evidence grounding, back-translation and
self-consistency. Each turns "the LLM said so" into a number the scoring engine can weigh.
"""

from __future__ import annotations

import re
import unicodedata
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


# PDF text is full of typographic variants of the same character; fold them so a correct
# quote matches whatever the extractor or the PDF happened to use.
_TYPOGRAPHY = str.maketrans(
    {
        **dict.fromkeys("\u2018\u2019\u02bc\u2032", "'"),
        **dict.fromkeys("\u201c\u201d\u2033", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        "\u00ad": None,  # soft hyphen
    }
)


def _clean(text: str) -> str:
    """NFKC (ligatures such as 'ﬁ', full-width forms) plus typographic folding."""
    return unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY)


def _norm(text: str) -> str:
    # lower(), not casefold(): quote tokens and source tokens must use the same folding.
    return canonical_text(_clean(text)).lower()


GROUNDING_FLOOR = 0.5  # raw similarity at or below this is noise, not partial support
ALTERED_CAP = 0.1  # a quote that changes a number, name or negation is not grounded
# Words, numbers with separators (1,200,000 / 31.12.2025 / LR-705) and contractions (didn't).
_TOKEN = re.compile(r"\w[\w\-./',]*")
_NEGATIONS = {"not", "no", "never", "without", "none", "neither", "nor", "cannot"}


def _tokens(text: str) -> list[str]:
    return [tok.rstrip(".,'") for tok in _TOKEN.findall(_clean(text))]


def _is_negation(token: str) -> bool:
    t = token.lower()
    return t in _NEGATIONS or t.endswith("n't")


def _missing_critical(quote: str, passage: str) -> list[str]:
    """Critical tokens of ``quote`` absent from ``passage`` (the matched source span).

    Critical: anything with a digit (years, amounts, LR-705), capitalised words (names,
    months; an abbreviation like "Dec" for "December" is accepted) and negations. Numbers,
    identifiers and negations must match exactly.
    """
    window = {t.lower() for t in _tokens(passage)}
    missing = []
    for raw in _tokens(quote):
        tok = raw.lower()
        if not (any(c.isdigit() for c in tok) or raw[:1].isupper() or _is_negation(tok)):
            continue
        if tok in window:
            continue
        exact_only = any(c.isdigit() for c in tok) or _is_negation(tok)
        if exact_only or not any(w.startswith(tok) for w in window):
            missing.append(tok)
    return missing


def _dropped_negation(quote: str, passage: str) -> bool:
    """The source passage negates something the quote states positively."""
    quoted = {t.lower() for t in _tokens(quote) if _is_negation(t)}
    return any(_is_negation(t) and t.lower() not in quoted for t in _tokens(passage))


PASSAGE_SLACK = 8  # chars: lets an abbreviation ("Dec" vs "December") keep the next token


def _expand(text: str, start: int, end: int) -> str:
    """Widen [start, end) by ``PASSAGE_SLACK`` and then to whole words. Kept small on purpose:
    a wider window would accept a year or name copied from elsewhere on the page."""
    start, end = max(0, start - PASSAGE_SLACK), min(len(text), end + PASSAGE_SLACK)
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end]


NEAR_QUOTE_CAP = 0.5  # a quote that isn't verbatim can never count as fully grounded
_CLAUSE_BREAK = re.compile(r"[.;:,!?]")


def _negated_before(source: str, start: int, quote: str) -> bool:
    """A negation in the two words before a verbatim match ("did not | comply with …")
    that the quote leaves out. Stops at clause punctuation, so "…none. The board approved"
    is context, not negation."""
    before = _CLAUSE_BREAK.split(source[:start])[-1]
    preceding = _tokens(before)[-2:]
    quoted = {t.lower() for t in _tokens(quote) if _is_negation(t)}
    return any(_is_negation(t) and t.lower() not in quoted for t in preceding)


_SENTENCE_BREAK = re.compile(r"[.;:!?]")


def _verbatim_score(source: str, start: int, quote: str) -> float:
    """Score one verbatim occurrence: 0.1 if a negation the quote drops sits in the two words
    right before it; 0.5 (review) if one appears earlier in the same sentence ("did not, in
    all respects, comply …"); otherwise 1.0."""
    if _negated_before(source, start, quote):
        return ALTERED_CAP
    sentence = _SENTENCE_BREAK.split(source[:start])[-1]
    quoted = {t.lower() for t in _tokens(quote) if _is_negation(t)}
    if any(_is_negation(t) and t.lower() not in quoted for t in _tokens(sentence)):
        return NEAR_QUOTE_CAP
    return 1.0


def grounding_score(quote: str, source_text: str | None) -> float:
    """How well the source text supports ``quote``, in [0, 1]. "It can miss, never invent":

    * **1.0 only for a verbatim quote**: after Unicode/typography/whitespace/case
      normalisation, on word *and number* boundaries ("LR-70" is not inside "LR-705",
      "$1,200" not inside "$1,200,000", "2025" not inside "2025/26"), not directly preceded
      by a negation the quote drops ("did not | comply" → 0.1), and with no such negation
      earlier in the same sentence ("did not, in all respects, comply" → 0.5). If the quote
      occurs several times, the best occurrence counts. Empty/punctuation-only quotes → 0.
    * **At most ``NEAR_QUOTE_CAP`` (0.5) for anything else**, however similar: the partial
      ratio rescaled above ``GROUNDING_FLOOR``, times 0.5. A one-word change ("Class A" for
      "Class B", "increased" for "decreased") can't be told from a typo by string similarity,
      so near-quotes are never trusted enough to auto-commit; they go to human review.
    * **At most ``ALTERED_CAP`` (0.1)** when the best-matching passage lacks a critical token
      of the quote (digits, capitalised names, negations) or negates what the quote states.
    * 0 when there is no source text.
    """
    if not source_text:
        return 0.0
    q, s = _norm(quote), _norm(source_text)
    if not re.search(r"\w", q):  # empty or punctuation-only after normalisation
        return 0.0
    pattern = r"(?<![\w\-])(?<!\d[.,/])" + re.escape(q) + r"(?![\w\-])(?![.,/]\d)"
    verbatim_scores = [_verbatim_score(s, m.start(), q) for m in re.finditer(pattern, s)]
    if verbatim_scores:
        return max(verbatim_scores)  # any un-negated occurrence supports the quote
    aligned = fuzz.partial_ratio_alignment(q, s)
    if aligned is None:
        return 0.0
    rescaled = max(0.0, (aligned.score / 100.0 - GROUNDING_FLOOR) / (1 - GROUNDING_FLOOR))
    score = NEAR_QUOTE_CAP * rescaled
    span = s[aligned.dest_start : aligned.dest_end]
    passage = _expand(s, aligned.dest_start, aligned.dest_end)
    if _missing_critical(quote, passage) or _dropped_negation(quote, span):
        score = min(score, ALTERED_CAP)
    return score


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
