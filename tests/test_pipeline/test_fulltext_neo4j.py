"""Full-text entity lookups against a real Neo4j (compose). Skipped unless WRITER_TEST_NEO4J_URI
is set. Creates the core schema (constraints + full-text indexes, which the app wants anyway)
and throwaway Issuer nodes, which it deletes.

Golden test (#28): the matcher gives the *same* results through the full-text index as it did
by reading every name (``InMemoryMatchIndex`` over the same nodes is the old behaviour).
"""

import os
import unicodedata
from collections.abc import Iterator
from typing import Any

import pytest
from neo4j import Driver, GraphDatabase

from demo_env.data import ISSUERS as DEMO_ISSUERS
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
ISSUERS = {
    f"{P}AUR": "Zqx Aurora Pacific Holdings",
    f"{P}AURL": "Zqx Aurora Pacific Holdings Ltd",  # near-twin: ambiguity must survive
    f"{P}BAN": "Zqx Banyan Logistics Group",
    f"{P}GRB": "Zqx Granite Bay Bank",
    f"{P}PAC": "Zqx Pacific Shipping",
    f"{P}OAK": "Zqx Oakwood Retail Trust",
    **{f"{P}N{i:03d}": f"Zqx Noise Company {i:03d} Holdings" for i in range(120)},
    # review #40: what only the analyzer tokenises (apostrophes, CJK, accents, dotted İ)
    f"{P}MCQ": "McQuillan's",
    f"{P}ZHB": "中华银行",
    f"{P}MUL": "Müller",
    f"{P}İŞBANK": "Zqx Isbank Turkiye",
    # review #40 round 2: what Python's NFKD would break (Hangul, kana voicing, ™)
    f"{P}SAM": "삼성전자",
    f"{P}HYU": "현대",
    f"{P}BRI": "ブリヂストン",
    f"{P}GAR": "ｶﾞﾙｰﾀﾞ",
    f"{P}삼성": "Zqx Samsung Key",
    f"{P}ACME™": "Zqx Acme Trademark",
    # review #40 round 3: all-caps Greek (final sigma), NFD-decomposed data
    f"{P}MYT": "ΜΥΤΙΛΗΝΑΙΟΣ",
    f"{P}ΟΔΟΣ": "Zqx Odos Key",
    f"{P}MU\u0308LLER": "Zqx Nfd Key",
    f"{P}HYD": unicodedata.normalize("NFD", "현대자동차"),
    # the demo issuers, under throwaway keys
    **{f"{P}D{i.ticker}": i.name for i in DEMO_ISSUERS},
    # a key whose token many longer keys share (more than TOP_K phrase hits)
    f"{P}TOPK": "Zqx Top K Exact",
    **{f"{P}TOPK-{i:02d}": f"Zqx Top K Variant {i:02d}" for i in range(60)},
}
QUERIES = [
    "Zqx Aurora Pacific Holdings",
    "zqx aurora pacific holdings ltd",
    "Zqx Aurora Pacfic Holdings",  # typo
    "Zqx Pacific",
    "Zqx Banyan Logistics",
    "ZQX GRANITE BAY BANK",
    "Zqx Oakwood Retail",
    "Zqx Noise Company 042 Holdings",
    "Zqx Totally Unknown Name",
    f"{P}gRb",  # a key in another case
    "McQuillan's",
    "中华银行",
    "Mueller",
    "Muller",
    f"{P}i̇şbank",
    "삼성전자",
    "현대",
    "ブリヂストン",
    "ｶﾞﾙｰﾀﾞ",
    f"{P.lower()}삼성",
    f"{P.lower()}acme™",
    "Μυτιληναιος",
    "ΜΥΤΙΛΗΝΑΙΟΣ",
    f"{P.lower()}οδος",
    f"{P.lower()}mu\u0308ller",
    unicodedata.normalize("NFD", "현대자동차"),
    f"{P}topk",
    *[i.name for i in DEMO_ISSUERS],
    *[i.name.replace(" (Demo)", "") for i in DEMO_ISSUERS],
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
            "UNWIND $rows AS r MERGE (n:Issuer {ticker: r.k}) SET n.name = r.n, n.stub = false",
            rows=[{"k": k, "n": n} for k, n in ISSUERS.items()],
        )
        d.execute_query("CALL db.awaitIndexes(120)")
        try:
            yield d
        finally:
            d.execute_query("MATCH (n:Issuer) WHERE n.ticker STARTS WITH $p DETACH DELETE n", p=P)


def old_behaviour_index(driver: Driver) -> InMemoryMatchIndex:
    """Every Issuer in the database, as the old full scan saw them."""
    idx = InMemoryMatchIndex()
    records, _, _ = driver.execute_query("MATCH (n:Issuer) RETURN n.ticker AS k, n.name AS name")
    for r in records:
        idx.add("Issuer", "ticker", str(r["k"]), r["name"])
    return idx


def results(index: Any, spec: OntologySpec) -> list[Any]:
    """Per query: status, key, confidence, the best alternative, and the scores of the
    alternatives that matter (at or above the ambiguity floor). Which *keys* fill the listed
    alternatives may differ only among equally scored names (e.g. 120 "Noise Company NNN"),
    where the full scan and the full-text top-k break the tie differently; weaker names
    never change a decision."""
    out = []
    for r in _results(index, spec):
        alts = [a for a in r.alternatives if a[1] >= AMBIGUITY_FLOOR]
        out.append((r.status, r.key, r.confidence, alts[:1], [round(a[1], 9) for a in alts]))
    return out


def _results(index: Any, spec: OntologySpec, queries: list[str] = QUERIES) -> list[Any]:
    m = EntityMatcher(index, spec)
    out: list[Any] = [m.resolve("Issuer", q) for q in queries]
    new_entities = [(f"{P}NEW", "Zqx Aurora Pacific Holdings"), (f"{P}X", "Zqx Brand New")]
    new_entities += [(f"{P}NEW2", "McQuillan's"), (f"{P}NEW3", "中华银行")]
    for key, name in new_entities:
        out.append(
            m.match_entity(
                CandidateEntity(class_name="Issuer", properties={"ticker": key, "name": name})
            )
        )
    return out


def test_fulltext_matching_gives_the_same_results_as_the_full_scan(
    driver: Driver, spec: OntologySpec
) -> None:
    expected = results(old_behaviour_index(driver), spec)
    recording = Recording(driver)
    actual = results(Neo4jMatchIndex(recording), spec)
    assert actual == expected
    statuses = {r[0] for r in expected}
    assert {"exact", "fuzzy", "ambiguous", "new", "possible_duplicate"} <= statuses  # golden spread
    assert not any("WHERE n.name IS NOT NULL" in s for s in recording.statements)  # no name scan
    # ASCII references never scan; non-ASCII *keys* are left to the scan on purpose
    ascii_only = Recording(driver)
    _results(Neo4jMatchIndex(ascii_only), spec, [q for q in QUERIES if q.isascii()])
    assert any("db.index.fulltext.queryNodes" in s for s in ascii_only.statements)
    assert not any("toLower(" in s for s in ascii_only.statements)


def test_case_insensitive_key_lookup_uses_the_index(driver: Driver) -> None:
    recording = Recording(driver)
    view = Neo4jGraphView(recording)
    props = view.node_properties("Issuer", "ticker", f"{P}aurl")
    assert props is not None and props["ticker"] == f"{P}AURL"  # not the AUR prefix-match
    assert view.node_properties("Issuer", "ticker", f"{P}nope") is None
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
