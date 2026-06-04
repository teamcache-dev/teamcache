from __future__ import annotations

import json
from pathlib import Path

import pytest

from teamcache.store import GitObjectStore, make_store
from teamcache.config import TeamCacheConfig
from teamcache.constants import SYMBOL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> GitObjectStore:
    summaries = tmp_path / "summaries"
    symbols = tmp_path / "symbols"
    return GitObjectStore(summaries, symbols)


def _summary_obj(key: str, file_path: str = "foo.py", summary_type: str = "static") -> dict:
    return {
        "cache_key": key,
        "file_path": file_path,
        "file_hash": "abc123",
        "summary": "Does something useful",
        "summary_type": summary_type,
        "language": "python",
        "schema_version": "v2",
    }


def _symbol_obj(key: str, file_path: str = "foo.py") -> dict:
    return {
        "cache_key": key,
        "file_path": file_path,
        "file_hash": "abc123",
        "language": "python",
        "symbols": {"functions": [], "classes": [], "imports": [], "exports": []},
    }


# ---------------------------------------------------------------------------
# write / read round-trip — summary objects
# ---------------------------------------------------------------------------

def test_write_creates_file(tmp_path):
    store = _store(tmp_path)
    key = "a" * 64
    obj = _summary_obj(key)
    store.write(key, obj)
    expected = tmp_path / "summaries" / key[:2] / f"{key[:12]}_v1.json"
    assert expected.exists()


def test_write_content_is_valid_json(tmp_path):
    store = _store(tmp_path)
    key = "b" * 64
    obj = _summary_obj(key)
    store.write(key, obj)
    path = tmp_path / "summaries" / key[:2] / f"{key[:12]}_v1.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == obj


def test_read_returns_written_object(tmp_path):
    store = _store(tmp_path)
    key = "c" * 64
    obj = _summary_obj(key)
    store.write(key, obj)
    result = store.read(key)
    assert result == obj


def test_read_missing_key_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.read("d" * 64) is None


def test_read_malformed_json_returns_none(tmp_path):
    store = _store(tmp_path)
    key = "e" * 64
    path = tmp_path / "summaries" / key[:2] / f"{key[:12]}_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert store.read(key) is None


def test_read_missing_required_field_returns_none(tmp_path):
    store = _store(tmp_path)
    key = "f" * 64
    obj = _summary_obj(key)
    del obj["summary_type"]
    path = tmp_path / "summaries" / key[:2] / f"{key[:12]}_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    assert store.read(key) is None


def test_write_raises_without_summary_type(tmp_path):
    store = _store(tmp_path)
    key = "g" * 64
    obj = _summary_obj(key)
    del obj["summary_type"]
    with pytest.raises(ValueError, match="summary_type"):
        store.write(key, obj)


def test_write_is_atomic_no_partial_file_on_disk(tmp_path):
    """The .tmp file must not remain after a successful write."""
    store = _store(tmp_path)
    key = "h" * 64
    store.write(key, _summary_obj(key))
    tmp_files = list((tmp_path / "summaries").glob("**/*.tmp"))
    assert tmp_files == []


# ---------------------------------------------------------------------------
# write_symbol / read_symbol round-trip
# ---------------------------------------------------------------------------

def test_write_symbol_creates_file(tmp_path):
    store = _store(tmp_path)
    key = "i" * 64
    obj = _symbol_obj(key)
    store.write_symbol(key, obj)
    expected = tmp_path / "symbols" / key[:2] / f"{key[:12]}_{SYMBOL_SCHEMA_VERSION}.json"
    assert expected.exists()


def test_read_symbol_returns_written_object(tmp_path):
    store = _store(tmp_path)
    key = "j" * 64
    obj = _symbol_obj(key)
    store.write_symbol(key, obj)
    assert store.read_symbol(key) == obj


def test_read_symbol_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.read_symbol("k" * 64) is None


def test_read_symbol_bad_symbols_field_returns_none(tmp_path):
    store = _store(tmp_path)
    key = "l" * 64
    obj = _symbol_obj(key)
    obj["symbols"] = "not-a-dict"
    path = tmp_path / "symbols" / key[:2] / f"{key[:12]}_{SYMBOL_SCHEMA_VERSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    assert store.read_symbol(key) is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_returns_written_keys(tmp_path):
    store = _store(tmp_path)
    keys = ["aa" + "0" * 62, "bb" + "1" * 62]
    for key in keys:
        store.write(key, _summary_obj(key))
    listed = store.list()
    assert len(listed) == 2
    for key in keys:
        expected_rel = f"{key[:2]}/{key[:12]}_v1.json"
        assert expected_rel in listed


def test_list_empty_when_nothing_written(tmp_path):
    store = _store(tmp_path)
    assert store.list() == []


def test_list_prefix_filters(tmp_path):
    store = _store(tmp_path)
    key_aa = "aa" + "2" * 62
    key_bb = "bb" + "3" * 62
    store.write(key_aa, _summary_obj(key_aa))
    store.write(key_bb, _summary_obj(key_bb))
    listed = store.list(prefix="aa/")
    assert len(listed) == 1
    assert listed[0].startswith("aa/")


# ---------------------------------------------------------------------------
# make_store
# ---------------------------------------------------------------------------

def test_make_store_returns_git_store(tmp_path):
    config = TeamCacheConfig()
    store = make_store(config, tmp_path)
    assert isinstance(store, GitObjectStore)


def test_make_store_unknown_backend_raises(tmp_path):
    config = TeamCacheConfig(objects_backend="s3")
    with pytest.raises(ValueError, match="s3"):
        make_store(config, tmp_path)


def test_make_store_git_store_paths(tmp_path):
    config = TeamCacheConfig()
    store = make_store(config, tmp_path)
    key = "m" * 64
    store.write(key, _summary_obj(key))
    # File must land under the canonical summaries directory
    expected = tmp_path / ".teamcache" / "objects" / "summaries" / key[:2] / f"{key[:12]}_v1.json"
    assert expected.exists()
