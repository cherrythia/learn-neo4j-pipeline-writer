import pytest

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef
from writer_service.pipeline.matching import (
    AMBIGUOUS_CONFIDENCE,
    EntityMatcher,
    InMemoryMatchIndex,
    name_similarity,
)


@pytest.fixture
def index() -> InMemoryMatchIndex:
    idx = InMemoryMatchIndex()
    idx.add("Issuer", "ticker", "APH", name="Aurora Pacific Holdings (Demo)")
    idx.add("Issuer", "ticker", "BYL", name="Banyan Logistics (Demo)")
    idx.add("Issuer", "ticker", "PSL", name="Pearl Shipping Lines (Demo)")
    idx.add("Issuer", "ticker", "PSX", name="Pearl Shipping Lines Holdings (Demo)")
    idx.add("Rule", "rule_code", "LR-705", name="Annual report timing")  # Rule has 'title'
    return idx


@pytest.fixture
def matcher(index: InMemoryMatchIndex, spec: OntologySpec) -> EntityMatcher:
    return EntityMatcher(index, spec)


def test_similarity_is_robust_to_case_punctuation_and_suffixes() -> None:
    assert name_similarity("aurora pacific holdings", "Aurora Pacific Holdings (Demo)") >= 0.9
    assert name_similarity("Pacific", "Aurora Pacific Holdings (Demo)") < 0.8  # subset of one word
    assert name_similarity("Granite Bay Bank", "Aurora Pacific Holdings") < 0.5


def test_exact_key_case_insensitive(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Issuer", "aph")
    assert (r.status, r.key, r.confidence) == ("exact", "APH", 1.0)


def test_name_resolves_to_existing_key(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Issuer", "Aurora Pacific Holdings")
    assert r.status == "fuzzy" and r.key == "APH"
    assert r.confidence is not None and 0.9 <= r.confidence < 1.0


def test_two_close_names_are_ambiguous(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Issuer", "Pearl Shipping Lines Holdings")
    assert (r.status, r.key) == ("fuzzy", "PSX")  # clearly closer to PSX than PSL
    r2 = matcher.resolve("Issuer", "Pearl Shipping")  # partial match to both
    assert r2.status == "ambiguous" and r2.key is None
    assert r2.confidence == AMBIGUOUS_CONFIDENCE
    assert {k for k, _ in r2.alternatives} >= {"PSL", "PSX"}


def test_unknown_is_new(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Issuer", "Zebra Mining")
    assert (r.status, r.key, r.confidence) == ("new", None, None)


def test_class_without_name_property_only_matches_exactly(matcher: EntityMatcher) -> None:
    assert matcher.resolve("Obligation", "7").status == "new"


def test_uses_title_when_class_has_no_name(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Rule", "annual report timing")
    assert r.status == "fuzzy" and r.key == "LR-705"


def test_entities_earlier_in_the_batch_are_matchable(matcher: EntityMatcher) -> None:
    matcher.add_batch_entity(
        CandidateEntity(class_name="Issuer", properties={"ticker": "ZBM", "name": "Zebra Mining"})
    )
    assert matcher.resolve("Issuer", "zbm").status == "exact"
    assert matcher.resolve("Issuer", "Zebra Mining Ltd").key == "ZBM"


def test_entity_candidate_exact_new_and_possible_duplicate(matcher: EntityMatcher) -> None:
    exact = matcher.match_entity(
        CandidateEntity(class_name="Issuer", properties={"ticker": "APH", "name": "x"})
    )
    assert exact.status == "exact" and exact.confidence == 1.0
    new = matcher.match_entity(
        CandidateEntity(class_name="Issuer", properties={"ticker": "NEW", "name": "Brand New Co"})
    )
    assert new.status == "new" and new.confidence is None
    dup = matcher.match_entity(
        CandidateEntity(
            class_name="Issuer", properties={"ticker": "AUR", "name": "Aurora Pacific Holdings"}
        )
    )
    assert dup.status == "possible_duplicate" and dup.key == "APH"
    assert dup.confidence is not None and dup.confidence <= 0.1  # very similar to someone else


def test_relation_endpoints_are_resolved_and_rewritten(matcher: EntityMatcher) -> None:
    rel = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="AR-2025-APH"),
        target=EntityRef(class_name="Issuer", key="Aurora Pacific Holdings"),
    )
    resolved, confidence, notes = matcher.resolve_relation(rel)
    assert resolved.target.key == "APH"
    assert resolved.source.key == "AR-2025-APH"  # new filing: unchanged
    assert confidence is not None and 0.9 <= confidence < 1.0  # weakest resolved endpoint
    assert any("Aurora Pacific Holdings" in n and "APH" in n for n in notes)


def test_relation_with_only_new_endpoints_has_neutral_confidence(matcher: EntityMatcher) -> None:
    rel = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="F9"),
        target=EntityRef(class_name="Issuer", key="Unknown Corp"),
    )
    _, confidence, _ = matcher.resolve_relation(rel)
    assert confidence is None


def test_relation_with_ambiguous_endpoint_is_not_rewritten(matcher: EntityMatcher) -> None:
    rel = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="F9"),
        target=EntityRef(class_name="Issuer", key="Pearl Shipping"),
    )
    resolved, confidence, notes = matcher.resolve_relation(rel)
    assert resolved.target.key == "Pearl Shipping"
    assert confidence == AMBIGUOUS_CONFIDENCE
    assert any("ambiguous" in n for n in notes)


def test_runner_up_within_margin_is_ambiguous(
    index: InMemoryMatchIndex, spec: OntologySpec
) -> None:
    index.add("Issuer", "ticker", "PS2", name="Pearl Shipping Lines Ltd")  # 0.955 vs PSL 0.944
    r = EntityMatcher(index, spec).resolve("Issuer", "Pearl Shipping Lines")
    assert r.status == "ambiguous"


def test_floor_and_threshold_are_configurable(
    index: InMemoryMatchIndex, spec: OntologySpec
) -> None:
    strict = EntityMatcher(index, spec, threshold=0.99, floor=0.98)
    assert strict.resolve("Issuer", "Aurora Pacific Holdings").status == "new"
