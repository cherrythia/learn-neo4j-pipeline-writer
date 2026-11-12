import pytest

from writer_service.ontology.spec import ClassSpec, OntologySpec, PropertySpec
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
    idx.add("Software", "software_id", "SW-QDOC", name="Quillstack Docs (Demo)")
    idx.add("Software", "software_id", "SW-BLDG", name="Brightledger Accounts (Demo)")
    idx.add("Software", "software_id", "SW-HSS", name="Harbor Sync Studio (Demo)")
    idx.add("Software", "software_id", "SW-HSE", name="Harbor Sync Studio Enterprise (Demo)")
    idx.add("Invoice", "invoice_number", "INV-2025-0418")  # Invoice has no name property
    return idx


@pytest.fixture
def matcher(index: InMemoryMatchIndex, spec: OntologySpec) -> EntityMatcher:
    return EntityMatcher(index, spec)


def test_similarity_is_robust_to_case_punctuation_and_suffixes() -> None:
    assert name_similarity("quillstack docs", "Quillstack Docs (Demo)") >= 0.9
    assert name_similarity("Docs", "Quillstack Docs (Demo)") < 0.8  # subset of one word
    assert name_similarity("Granite Bay Mail", "Quillstack Docs") < 0.5


def test_exact_key_case_insensitive(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Software", "sw-qdoc")
    assert (r.status, r.key, r.confidence) == ("exact", "SW-QDOC", 1.0)


def test_name_resolves_to_existing_key(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Software", "Quillstack Docs")
    assert r.status == "fuzzy" and r.key == "SW-QDOC"
    assert r.confidence is not None and 0.9 <= r.confidence < 1.0


@pytest.mark.parametrize(
    "class_name,key_prop,key,name,reference",
    [
        ("Asset", "asset_tag", "AS-25-0001", "Boardroom Display (Demo)", "Boardroom Display"),
        (
            "HardwareAsset",
            "asset_tag",
            "HW-25-0101",
            "Engineering Laptop 14 (Demo)",
            "Engineering Laptop 14",
        ),
        (
            "SoftwareLicense",
            "asset_tag",
            "SL-25-0042",
            "Quillstack Docs Enterprise (Demo)",
            "Quillstack Docs Enterprise",
        ),
        ("Software", "software_id", "SW-QDOC", "Quillstack Docs (Demo)", "Quillstack Docs"),
        ("Application", "app_code", "APP-ERP", "Finance ERP (Demo)", "Finance ERP"),
        ("Department", "dept_code", "FIN", "Finance Department (Demo)", "Finance Department"),
        (
            "Vendor",
            "name",
            "Zorblax Systems (Demo)",
            "Zorblax Systems (Demo)",
            "Zorblax Systems",
        ),
    ],
)
def test_every_named_class_resolves_by_name(
    spec: OntologySpec, class_name: str, key_prop: str, key: str, name: str, reference: str
) -> None:
    idx = InMemoryMatchIndex()
    idx.add(class_name, key_prop, key, name=name)
    r = EntityMatcher(idx, spec).resolve(class_name, reference)
    assert (r.status, r.key) == ("fuzzy", key)


def test_two_close_names_are_ambiguous(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Software", "Harbor Sync Studio Enterprise")
    assert (r.status, r.key) == ("fuzzy", "SW-HSE")  # clearly closer to SW-HSE than SW-HSS
    r2 = matcher.resolve("Software", "Harbor Studio")  # partial match to both
    assert r2.status == "ambiguous" and r2.key is None
    assert r2.confidence == AMBIGUOUS_CONFIDENCE
    assert {k for k, _ in r2.alternatives} >= {"SW-HSS", "SW-HSE"}


def test_unknown_is_new(matcher: EntityMatcher) -> None:
    r = matcher.resolve("Software", "Zebra Paint")
    assert (r.status, r.key, r.confidence) == ("new", None, None)


def test_class_without_name_property_only_matches_exactly(matcher: EntityMatcher) -> None:
    assert matcher.resolve("Invoice", "INV-2025-0418 (Zorblax)").status == "new"
    assert matcher.resolve("Invoice", "inv-2025-0418").status == "exact"


def test_uses_title_when_class_has_no_name(index: InMemoryMatchIndex, spec: OntologySpec) -> None:
    """No core ITAM class has a ``title``; a class that does is matched on it."""
    article = ClassSpec(
        "KnowledgeArticle",
        "article_id",
        (PropertySpec("article_id", "string", True), PropertySpec("title", "string")),
    )
    with_title = OntologySpec((*spec.classes, article), spec.relations)
    index.add("KnowledgeArticle", "article_id", "KB-0012", name="Laptop refresh procedure")
    r = EntityMatcher(index, with_title).resolve("KnowledgeArticle", "laptop refresh procedure")
    assert r.status == "fuzzy" and r.key == "KB-0012"


def test_entities_earlier_in_the_batch_are_matchable(matcher: EntityMatcher) -> None:
    matcher.add_batch_entity(
        CandidateEntity(
            class_name="Software", properties={"software_id": "SW-ZBP", "name": "Zebra Paint"}
        )
    )
    assert matcher.resolve("Software", "sw-zbp").status == "exact"
    assert matcher.resolve("Software", "Zebra Paint Pro").key == "SW-ZBP"


def test_entity_candidate_exact_new_and_possible_duplicate(matcher: EntityMatcher) -> None:
    exact = matcher.match_entity(
        CandidateEntity(class_name="Software", properties={"software_id": "SW-QDOC", "name": "x"})
    )
    assert exact.status == "exact" and exact.confidence == 1.0
    new = matcher.match_entity(
        CandidateEntity(
            class_name="Software", properties={"software_id": "SW-NEW", "name": "Brand New Tool"}
        )
    )
    assert new.status == "new" and new.confidence is None
    dup = matcher.match_entity(
        CandidateEntity(
            class_name="Software", properties={"software_id": "SW-QD2", "name": "Quillstack Docs"}
        )
    )
    assert dup.status == "possible_duplicate" and dup.key == "SW-QDOC"
    assert dup.confidence is not None and dup.confidence <= 0.1  # very similar to someone else


def licenses(license_tag: str, software: str) -> CandidateRelation:
    return CandidateRelation(
        relation="LICENSES",
        source=EntityRef(class_name="SoftwareLicense", key=license_tag),
        target=EntityRef(class_name="Software", key=software),
    )


def test_relation_endpoints_are_resolved_and_rewritten(matcher: EntityMatcher) -> None:
    resolved, confidence, notes = matcher.resolve_relation(
        licenses("SL-25-0042", "Quillstack Docs")
    )
    assert resolved.target.key == "SW-QDOC"
    assert resolved.source.key == "SL-25-0042"  # new licence: unchanged
    assert confidence is not None and 0.9 <= confidence < 1.0  # weakest resolved endpoint
    assert any("Quillstack Docs" in n and "SW-QDOC" in n for n in notes)


def test_relation_with_only_new_endpoints_has_neutral_confidence(matcher: EntityMatcher) -> None:
    _, confidence, _ = matcher.resolve_relation(licenses("SL9", "Unknown Tool"))
    assert confidence is None


def test_relation_with_ambiguous_endpoint_is_not_rewritten(matcher: EntityMatcher) -> None:
    resolved, confidence, notes = matcher.resolve_relation(licenses("SL9", "Harbor Studio"))
    assert resolved.target.key == "Harbor Studio"
    assert confidence == AMBIGUOUS_CONFIDENCE
    assert any("ambiguous" in n for n in notes)


def test_runner_up_within_margin_is_ambiguous(
    index: InMemoryMatchIndex, spec: OntologySpec
) -> None:
    index.add("Software", "software_id", "SW-HS2", name="Harbor Sync Studio Ltd")  # 0.950 vs 0.939
    r = EntityMatcher(index, spec).resolve("Software", "Harbor Sync Studio")
    assert r.status == "ambiguous"


def test_floor_and_threshold_are_configurable(
    index: InMemoryMatchIndex, spec: OntologySpec
) -> None:
    strict = EntityMatcher(index, spec, threshold=0.99, floor=0.98)
    assert strict.resolve("Software", "Quillstack Docs").status == "new"


def test_case_only_key_difference_is_rewritten_to_stored_key(matcher: EntityMatcher) -> None:
    resolved, confidence, notes = matcher.resolve_relation(licenses("SL9", "sw-qdoc"))
    assert resolved.target.key == "SW-QDOC" and confidence == 1.0
    assert any("normalised to 'SW-QDOC'" in n for n in notes)


def test_match_entity_returns_the_stored_key(matcher: EntityMatcher) -> None:
    r = matcher.match_entity(
        CandidateEntity(class_name="Software", properties={"software_id": "sw-qdoc", "name": "x"})
    )
    assert (r.status, r.key) == ("exact", "SW-QDOC")


def test_reset_batch_forgets_previous_batch(matcher: EntityMatcher) -> None:
    matcher.add_batch_entity(
        CandidateEntity(
            class_name="Software", properties={"software_id": "SW-ZBP", "name": "Zebra Paint"}
        )
    )
    assert matcher.resolve("Software", "sw-zbp").status == "exact"
    matcher.reset_batch()
    assert matcher.resolve("Software", "sw-zbp").status == "new"


class CountingIndex(InMemoryMatchIndex):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def candidates(
        self, label: str, key_prop: str, name_prop: str, text: str
    ) -> list[tuple[str, str]]:
        self.queries.append(text)
        return super().candidates(label, key_prop, name_prop, text)


def test_name_candidates_are_queried_once_per_reference_per_batch(spec: OntologySpec) -> None:
    """#28: the index is asked for candidates *for this reference* (Neo4j: full-text top-k)
    instead of the matcher reading every name of the class; repeats are cached per batch."""
    idx = CountingIndex()
    idx.add("Software", "software_id", "SW-QDOC", name="Quillstack Docs (Demo)")
    m = EntityMatcher(idx, spec)
    for text in ("Quillstack Docs", "Brightledger", "quillstack docs", "Quillstack Docs"):
        m.resolve("Software", text)
    assert idx.queries == ["Quillstack Docs", "Brightledger"]
    m.reset_batch()
    m.resolve("Software", "Quillstack Docs")
    assert idx.queries[-1] == "Quillstack Docs" and len(idx.queries) == 3
