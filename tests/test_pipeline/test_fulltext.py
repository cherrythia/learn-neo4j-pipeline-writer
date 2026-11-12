"""Lucene query building for the Neo4j full-text lookups (#28)."""

from writer_service.pipeline.neo4j_lookup import MAX_WORDS, fold, key_query, name_query


def test_key_query_is_a_lower_cased_field_scoped_phrase() -> None:
    assert key_query("dept_code", "FIN") == 'dept_code:"fin"'
    assert key_query("asset_tag", "HW-25-0101") == 'asset_tag:"hw-25-0101"'
    assert key_query("dept_code", "IT-OPS") == 'dept_code:"it-ops"'


def test_keys_the_index_cant_be_trusted_with_are_left_to_the_scan() -> None:
    """Review #40 round 3: non-ASCII keys (lower-casing differs, e.g. a final sigma) and
    keys made only of stop words (no tokens in the index)."""
    for key in ("İŞLEM", "한빛", "Acme™", "ΟΔΟΣ", "IT", "on", "A", "a.it"):
        assert key_query("dept_code", key) is None, key


def test_key_query_escapes_phrase_metacharacters() -> None:
    assert key_query("id", 'x "b" \\c') == 'id:"x \\"b\\" \\\\c"'


def test_key_query_without_word_characters_is_none() -> None:
    assert key_query("id", "--") is None and key_query("id", "") is None


def terms(query: str | None) -> list[str]:
    assert query is not None
    fuzzy = query.split(" OR name:(")[-1].rstrip(")")
    return fuzzy.split(" OR ")


def test_name_query_is_the_analysed_text_or_fuzzy_terms() -> None:
    q = name_query("name", "Quillstack Docs (Demo) Co")
    assert q is not None and q.startswith(r"name:(quillstack docs \(demo\) co) OR ")
    assert {"quillstack~1", "docs~1", "demo~1", "quillstackdocs~1"} <= set(terms(q))


def test_name_query_keeps_what_only_the_analyzer_tokenises() -> None:
    """Review #40: apostrophes, CJK and accents must reach the analyzer."""
    assert name_query("name", "McQuillan's").startswith("name:(mcquillan's) OR ")  # type: ignore[union-attr]
    assert name_query("name", "中华银行") == "name:(中华银行)"  # CJK: no fuzzy terms
    assert name_query("name", "ガラパゴス") == "name:(ガラパゴス)"  # voicing marks kept
    q = name_query("name", "Müller Software")
    assert q is not None and q.startswith("name:(müller software) OR ")
    assert {"müller~1", "muller~1", "software~1"} <= set(terms(q))


def test_name_query_covers_greek_and_cyrillic_words() -> None:
    """Review #40 round 4: all-caps Greek drops accents; Cyrillic typos."""
    q = name_query("name", "ΚΥΚΛΩΠΑΣ")
    assert q is not None and q.startswith("name:(κυκλωπας) OR name:(κυκλωπασ) OR ")
    assert "κυκλωπασ~1" in terms(q)
    assert "звезда~1" in terms(name_query("name", "Звезда"))


def test_name_query_joins_adjacent_words_and_prefixes_long_ones() -> None:
    """Review #40 round 4: "Quill Stack" vs "QuillStack", "QuillStackz" vs "Quill Stackz"."""
    assert "quillstack~1" in terms(name_query("name", "Quill Stack"))
    assert "quill*" in terms(name_query("name", "QuillStackz"))


def test_name_query_adds_the_sigma_the_index_stores() -> None:
    q = name_query("name", "ΟΜΙΛΟΣ")
    assert q is not None and q.startswith("name:(ομιλος) OR name:(ομιλοσ) OR ")


def test_name_query_operators_and_specials_are_harmless() -> None:
    q = name_query("name", "Smith AND Sons")
    assert q is not None and q.startswith("name:(smith and sons) OR ")
    assert name_query("name", 'a+b: "c"~ [d]') == r"name:(a\+b\: \"c\"\~ \[d\])"
    assert name_query("name", "!!") is None


def test_name_query_caps_fuzzy_words() -> None:
    text = " ".join(f"word{i:04d}" for i in range(1100))
    query = name_query("name", text)
    assert query is not None and len(terms(query)) == MAX_WORDS * 4
    assert query.count("word") < 300  # well under Lucene's 1024-clause limit


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
        self.scan_below = 0  # always the full-text path
        self.fulltext, self.scan, self.ran = fulltext, scan, []
        self._fulltext_cache = {
            ("Department", "dept_code"): "wsf_d",
            ("Department", "name"): "wsf_d",
        }

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
    others = [{"node": {"dept_code": f"TOPK-{i}"}} for i in range(TOP_K)]
    look = Scripted(others, scan=[{"n": {"dept_code": "TOPK"}}])
    assert look._find_node("Department", "dept_code", "topk") == {"dept_code": "TOPK"}
    assert any("toLower" in c for c in look.ran)


def test_an_uncapped_miss_is_trusted_without_a_scan() -> None:
    look = Scripted([{"node": {"dept_code": "TOPK-1"}}], scan=[{"n": {"dept_code": "x"}}])
    assert look._find_node("Department", "dept_code", "topk") is None
    assert not any("toLower" in c for c in look.ran)


def test_a_failing_fulltext_query_falls_back_to_the_scan() -> None:
    look = Scripted(ClientError("too many clauses"), scan=[{"k": "FIN", "name": "Finance"}])
    assert look._name_candidates("Department", "dept_code", "name", "Finance") == [
        ("FIN", "Finance")
    ]


def test_small_labels_use_the_old_scan() -> None:
    """Below ``scan_below`` nodes the exact old behaviour is kept; the index is for scale."""
    look = Scripted([{"node": {"dept_code": "X"}}], scan=[{"n": {"dept_code": "FIN"}}])
    look.scan_below = 10
    look.__dict__["_label_counts"] = {"Department": 3}
    assert look._find_node("Department", "dept_code", "fin") == {"dept_code": "FIN"}
    assert not any("queryNodes" in c for c in look.ran)


def test_the_full_name_read_happens_once_per_batch() -> None:
    """Review #40 round 5: small (scanned) classes read every name once, not per reference."""
    look = Scripted([], scan=[{"k": "FIN", "name": "Finance"}])
    look.scan_below = 10
    look.__dict__["_label_counts"] = {"Department": 3}
    for text in ("Finance", "Facilities", "Zebra"):
        assert look._name_candidates("Department", "dept_code", "name", text) == [
            ("FIN", "Finance")
        ]
    assert sum("IS NOT NULL" in c for c in look.ran) == 1
