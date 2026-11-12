"""Lucene query building for the Neo4j full-text lookups (#28)."""

from writer_service.pipeline.neo4j_lookup import key_query, name_query


def test_key_query_is_a_field_scoped_phrase() -> None:
    assert key_query("ticker", "APH") == 'ticker:"aph"'
    assert key_query("filing_id", "AR-2025-APH") == 'filing_id:"ar-2025-aph"'


def test_key_query_escapes_phrase_metacharacters() -> None:
    assert key_query("id", 'a "b" \\c') == 'id:"a \\"b\\" \\\\c"'


def test_key_query_without_word_characters_is_none() -> None:
    assert key_query("id", "--") is None and key_query("id", "") is None


def test_name_query_ors_tokens_with_fuzzy_long_ones() -> None:
    assert name_query("name", "Aurora Pacific (Demo) Co") == (
        "name:(aurora~1 OR pacific~1 OR demo~1 OR co)"
    )
    assert name_query("name", "AND or NOT") == "name:(and OR or OR not)"  # lower-case: no operators
    assert name_query("name", "!!") is None
