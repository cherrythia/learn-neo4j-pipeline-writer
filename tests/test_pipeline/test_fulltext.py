"""Lucene query building for the Neo4j full-text lookups (#28)."""

from writer_service.pipeline.neo4j_lookup import MAX_WORDS, fold, key_query, name_query


def test_key_query_is_a_lower_cased_field_scoped_phrase() -> None:
    assert key_query("ticker", "APH") == 'ticker:"aph"'
    assert key_query("filing_id", "AR-2025-APH") == 'filing_id:"ar-2025-aph"'
    assert key_query("ticker", "IT-HOLD") == 'ticker:"it-hold"'


def test_keys_the_index_cant_be_trusted_with_are_left_to_the_scan() -> None:
    """Review #40 round 3: non-ASCII keys (lower-casing differs, e.g. a final sigma) and
    keys made only of stop words (no tokens in the index)."""
    for key in ("İŞBANK", "삼성", "Acme™", "ΟΔΟΣ", "IT", "on", "A", "a.it"):
        assert key_query("ticker", key) is None, key


def test_key_query_escapes_phrase_metacharacters() -> None:
    assert key_query("id", 'x "b" \\c') == 'id:"x \\"b\\" \\\\c"'


def test_key_query_without_word_characters_is_none() -> None:
    assert key_query("id", "--") is None and key_query("id", "") is None


def test_name_query_is_the_analysed_text_or_folded_fuzzy_words() -> None:
    assert name_query("name", "Aurora Pacific (Demo) Co") == (
        r"name:(aurora pacific \(demo\) co) OR name:(aurora~1 OR pacific~1 OR demo~1)"
    )


def test_name_query_keeps_what_only_the_analyzer_tokenises() -> None:
    """Review #40: apostrophes, CJK and accents must reach the analyzer."""
    assert name_query("name", "McQuillan's") == "name:(mcquillan's) OR name:(mcquillan~1)"
    assert name_query("name", "中华银行") == "name:(中华银行)"  # no ASCII words: no fuzzy
    assert name_query("name", "ブリヂストン") == "name:(ブリヂストン)"  # voicing marks kept
    assert name_query("name", "Müller Bank") == "name:(müller bank) OR name:(muller~1 OR bank~1)"


def test_name_query_adds_the_sigma_the_index_stores() -> None:
    assert name_query("name", "ΟΜΙΛΟΣ") == "name:(ομιλος) OR name:(ομιλοσ)"


def test_name_query_operators_and_specials_are_harmless() -> None:
    assert name_query("name", "Smith AND Sons") == (
        "name:(smith and sons) OR name:(smith~1 OR sons~1)"
    )
    assert name_query("name", 'a+b: "c"~ [d]') == r"name:(a\+b\: \"c\"\~ \[d\])"
    assert name_query("name", "!!") is None


def test_name_query_caps_fuzzy_words() -> None:
    text = " ".join(f"word{i:04d}" for i in range(1100))
    query = name_query("name", text)
    assert query is not None and query.count("~1") == MAX_WORDS
    assert query.count("word") < 200  # the analysed clause is capped too (1024-clause limit)


def test_fold_matches_ascii_folding() -> None:
    assert fold("Müller ÉCOLE") == "muller ecole"


# ---------------------------------------------------------------- fallbacks (review #40)

from typing import Any  # noqa: E402

from neo4j.exceptions import ClientError  # noqa: E402

from writer_service.pipeline.neo4j_lookup import TOP_K, Neo4jLookup  # noqa: E402


class Scripted(Neo4jLookup):
    """Answers each kind of statement from a script, records what ran."""

    def __init__(self, fulltext: Any, scan: list[Any]) -> None:
        super().__init__(driver=None)
        self.fulltext, self.scan, self.ran = fulltext, scan, []
        self._fulltext_cache = {("Issuer", "ticker"): "wsf_i", ("Issuer", "name"): "wsf_i"}

    def _query(self, cypher: str, **params: Any) -> list[Any]:
        self.ran.append(cypher)
        if "queryNodes" in cypher:
            if isinstance(self.fulltext, Exception):
                raise self.fulltext
            return self.fulltext
        if "toLower" in cypher or "IS NOT NULL" in cypher:
            return self.scan
        return []  # the exact-key lookup misses


def test_a_capped_key_result_without_an_exact_hit_falls_back_to_the_scan() -> None:
    others = [{"node": {"ticker": f"TOPK-{i}"}} for i in range(TOP_K)]
    look = Scripted(others, scan=[{"n": {"ticker": "TOPK"}}])
    assert look._find_node("Issuer", "ticker", "topk") == {"ticker": "TOPK"}
    assert any("toLower" in c for c in look.ran)


def test_an_uncapped_miss_is_trusted_without_a_scan() -> None:
    look = Scripted([{"node": {"ticker": "TOPK-1"}}], scan=[{"n": {"ticker": "x"}}])
    assert look._find_node("Issuer", "ticker", "topk") is None
    assert not any("toLower" in c for c in look.ran)


def test_a_failing_fulltext_query_falls_back_to_the_scan() -> None:
    look = Scripted(ClientError("too many clauses"), scan=[{"k": "APH", "name": "Aurora"}])
    assert look._name_candidates("Issuer", "ticker", "name", "Aurora") == [("APH", "Aurora")]
