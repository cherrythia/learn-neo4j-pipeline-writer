"""Full-text entity lookups against a real Neo4j (compose). Skipped unless WRITER_TEST_NEO4J_URI
is set. Creates the core schema (constraints + full-text indexes, which the app wants anyway)
and throwaway Department nodes (dept_code prefixed ``__FT_``), which it deletes.

Golden test (#28): the matcher gives the *same* results through the full-text index as it did
by reading every name (``InMemoryMatchIndex`` over the same nodes is the old behaviour).
"""

import os
import unicodedata
from collections.abc import Iterator
from typing import Any

import pytest
from neo4j import Driver, GraphDatabase

from demo_env.data import DEPARTMENTS as DEMO_DEPARTMENTS
from writer_service.ontology.neo4j_schema import Neo4jGraphSchema, sync_schema
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity
from writer_service.pipeline.graph_view import Neo4jGraphView
from writer_service.pipeline.matching import (
    AMBIGUITY_FLOOR,
    EntityMatcher,
    InMemoryMatchIndex,
    Neo4jMatchIndex,
)

pytestmark = pytest.mark.integration
P = "__FT_"
DEPARTMENTS = {
    f"{P}FSS": "Zqx Finance Shared Services",
    f"{P}FSSL": "Zqx Finance Shared Services Ltd",  # near-twin: ambiguity must survive
    f"{P}LOG": "Zqx Harbor Logistics Group",
    f"{P}FAC": "Zqx Granite Bay Facilities",
    f"{P}PRC": "Zqx Shared Procurement",
    f"{P}OPS": "Zqx Oakwood Retail Operations",
    **{f"{P}N{i:03d}": f"Zqx Noise Department {i:03d} Services" for i in range(120)},
    # review #40: what only the analyzer tokenises (apostrophes, CJK, accents, dotted İ)
    f"{P}MCQ": "McQuillan's",
    f"{P}CWB": "财务部",
    f"{P}MUL": "Müller",
    f"{P}İŞLEM": "Zqx Islem Key",
    # review #40 round 2: what Python's NFKD would break (Hangul, kana voicing, ™)
    f"{P}PSJ": "푸른숲정보",
    f"{P}SBK": "새벽",
    f"{P}GLP": "ガラパゴス",
    f"{P}PGD": "ﾊﾟｺﾞﾀﾞ",
    f"{P}새벽": "Zqx Hangul Key",
    f"{P}ACME™": "Zqx Acme Trademark",
    # review #40 round 3: all-caps Greek (final sigma), NFD-decomposed data
    f"{P}KYK": "ΚΥΚΛΩΠΑΣ",
    f"{P}ΟΔΟΣ": "Zqx Odos Key",
    f"{P}MÜLLER": "Zqx Nfd Key",
    f"{P}SBJ": unicodedata.normalize("NFD", "새벽자동차"),
    # review #40 round 4: accents dropped in all-caps Greek, a Cyrillic typo, one word vs two
    f"{P}KYKA": "Κύκλωπας",
    f"{P}THE": "ΘΕΑΤΡΟ",
    f"{P}ZVZ": "Зквзвездапром",
    f"{P}AQQ": "Aqquillstack",
    f"{P}AQH": "Aqharbor Synkz",
    # the demo departments, under throwaway keys
    **{f"{P}D{d.dept_code}": d.name for d in DEMO_DEPARTMENTS},
    # a key whose token many longer keys share (more than TOP_K phrase hits)
    f"{P}TOPK": "Zqx Top K Exact",
    **{f"{P}TOPK-{i:02d}": f"Zqx Top K Variant {i:02d}" for i in range(60)},
}
QUERIES = [
    "Zqx Finance Shared Services",
    "zqx finance shared services ltd",
    "Zqx Finance Sharde Services",  # typo
    "Zqx Shared",
    "Zqx Harbor Logistics",
    "ZQX GRANITE BAY FACILITIES",
    "Zqx Oakwood Retail",
    "Zqx Noise Department 042 Services",
    "Zqx Totally Unknown Name",
    f"{P}fAc",  # a key in another case
    "McQuillan's",
    "财务部",
    "Mueller",
    "Muller",
    f"{P}{'İŞLEM'.lower()}",  # dotted İ lower-cases to i + combining dot
    "푸른숲정보",
    "새벽",
    "ガラパゴス",
    "ﾊﾟｺﾞﾀﾞ",
    f"{P.lower()}새벽",
    f"{P.lower()}acme™",
    "Κυκλωπας",
    "ΚΥΚΛΩΠΑΣ",
    f"{P.lower()}οδος",
    f"{P.lower()}müller",
    unicodedata.normalize("NFD", "새벽자동차"),
    "Θέατρο",
    "Зквзвездапрон",
    "Aqquill Stack",
    "Aqharborsynkz",
    f"{P}topk",
    *[d.name for d in DEMO_DEPARTMENTS],
    *[f"{d.name} Department" for d in DEMO_DEPARTMENTS],
]


class Recording:
    """Driver proxy that records every Cypher statement."""

    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.statements: list[str] = []

    def execute_query(self, cypher: str, **kw: Any) -> Any:
        self.statements.append(cypher)
        return self.driver.execute_query(cypher, **kw)


@pytest.fixture
def driver(spec: OntologySpec) -> Iterator[Driver]:
    uri = os.environ.get("WRITER_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("WRITER_TEST_NEO4J_URI not set")
    auth = (
        os.environ.get("WRITER_TEST_NEO4J_USER", "neo4j"),
        os.environ.get("WRITER_TEST_NEO4J_PASSWORD", ""),
    )
    with GraphDatabase.driver(uri, auth=auth) as d:
        sync_schema(Neo4jGraphSchema(d), spec)
        d.execute_query(
            "UNWIND $rows AS r MERGE (n:Department {dept_code: r.k}) "
            "SET n.name = r.n, n.stub = false",
            rows=[{"k": k, "n": n} for k, n in DEPARTMENTS.items()],
        )
        d.execute_query("CALL db.awaitIndexes(120)")
        try:
            yield d
        finally:
            d.execute_query(
                "MATCH (n:Department) WHERE n.dept_code STARTS WITH $p DETACH DELETE n", p=P
            )


def old_behaviour_index(driver: Driver) -> InMemoryMatchIndex:
    """Every Department in the database, as the old full scan saw them."""
    idx = InMemoryMatchIndex()
    records, _, _ = driver.execute_query(
        "MATCH (n:Department) RETURN n.dept_code AS k, n.name AS name"
    )
    for r in records:
        idx.add("Department", "dept_code", str(r["k"]), r["name"])
    return idx


def indexed(lookup: Any) -> Any:
    """Use the full-text path even on this small test graph (below ``SCAN_BELOW`` nodes a
    class is scanned, exactly as before #28)."""
    lookup.scan_below = 0
    return lookup


def results(index: Any, spec: OntologySpec) -> list[Any]:
    """Per query: status, key, confidence, the best alternative, and the scores of the
    alternatives that matter (at or above the ambiguity floor). Which *keys* fill the listed
    alternatives may differ only among equally scored names (e.g. 120 "Noise Department NNN"),
    where the full scan and the full-text top-k break the tie differently; weaker names
    never change a decision."""
    out = []
    for r in _results(index, spec):
        alts = [a for a in r.alternatives if a[1] >= AMBIGUITY_FLOOR]
        out.append((r.status, r.key, r.confidence, alts[:1], [round(a[1], 9) for a in alts]))
    return out


def _results(index: Any, spec: OntologySpec, queries: list[str] = QUERIES) -> list[Any]:
    m = EntityMatcher(index, spec)
    out: list[Any] = [m.resolve("Department", q) for q in queries]
    new_entities = [(f"{P}NEW", "Zqx Finance Shared Services"), (f"{P}X", "Zqx Brand New")]
    new_entities += [(f"{P}NEW2", "McQuillan's"), (f"{P}NEW3", "财务部")]
    for key, name in new_entities:
        out.append(
            m.match_entity(
                CandidateEntity(
                    class_name="Department", properties={"dept_code": key, "name": name}
                )
            )
        )
    return out


def test_fulltext_matching_gives_the_same_results_as_the_full_scan(
    driver: Driver, spec: OntologySpec
) -> None:
    expected = results(old_behaviour_index(driver), spec)
    recording = Recording(driver)
    actual = results(indexed(Neo4jMatchIndex(recording)), spec)
    assert actual == expected
    statuses = {r[0] for r in expected}
    assert {"exact", "fuzzy", "ambiguous", "new", "possible_duplicate"} <= statuses  # golden spread
    assert not any("WHERE n.name IS NOT NULL" in s for s in recording.statements)  # no name scan
    # ASCII references never scan; non-ASCII *keys* are left to the scan on purpose
    ascii_only = Recording(driver)
    _results(indexed(Neo4jMatchIndex(ascii_only)), spec, [q for q in QUERIES if q.isascii()])
    assert any("db.index.fulltext.queryNodes" in s for s in ascii_only.statements)
    assert not any("toLower(" in s for s in ascii_only.statements)


def test_case_insensitive_key_lookup_uses_the_index(driver: Driver) -> None:
    recording = Recording(driver)
    view = indexed(Neo4jGraphView(recording))
    props = view.node_properties("Department", "dept_code", f"{P}fssl")
    assert props is not None and props["dept_code"] == f"{P}FSSL"  # not the FSS prefix-match
    assert view.node_properties("Department", "dept_code", f"{P}nope") is None
    assert not any("toLower(" in s for s in recording.statements)


def test_without_the_index_it_falls_back_to_scanning(driver: Driver, spec: OntologySpec) -> None:
    """A class without an ONLINE full-text index (e.g. not yet synced) still matches."""

    class NoIndex(Neo4jMatchIndex):
        def _fulltext_index(self, label: str, field: str) -> str | None:
            return None

    expected = results(old_behaviour_index(driver), spec)
    recording = Recording(driver)
    assert results(NoIndex(recording), spec) == expected
    assert not any("db.index.fulltext" in s for s in recording.statements)


def test_small_classes_keep_the_old_scan(driver: Driver, spec: OntologySpec) -> None:
    """With the default threshold this ~250-department graph is scanned: identical to before."""
    recording = Recording(driver)
    assert results(Neo4jMatchIndex(recording), spec) == results(old_behaviour_index(driver), spec)
    assert not any("db.index.fulltext.queryNodes" in s for s in recording.statements)
