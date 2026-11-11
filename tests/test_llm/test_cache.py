from pathlib import Path

from writer_service.llm.cache import FileCache, InMemoryCache


def test_in_memory_roundtrip() -> None:
    c = InMemoryCache()
    assert c.get("k") is None
    c.put("k", {"data": {"a": 1}, "model": "m"})
    assert c.get("k") == {"data": {"a": 1}, "model": "m"}


def test_file_cache_persists_across_instances(tmp_path: Path) -> None:
    FileCache(tmp_path).put("abc12345", {"data": {"x": [1, 2]}, "model": "m"})
    assert FileCache(tmp_path).get("abc12345") == {"data": {"x": [1, 2]}, "model": "m"}


def test_file_cache_rejects_path_like_keys(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        FileCache(tmp_path).put("../evil", {})
