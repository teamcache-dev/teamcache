from __future__ import annotations

from pathlib import Path

import pytest

from teamcache.db import (
    connect,
    coverage_stats,
    find_symbol_by_name,
    get_summary,
    get_summary_by_path,
    insert_summary,
    insert_symbol,
    invalidate_by_path,
    invalidate_stale,
    keyword_search,
    static_only_files,
    top_contributors,
)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "index.sqlite")
    yield c
    c.close()


def _summary(cache_key="abc123", file_path="src/foo.py", summary_type="static", summary="imports: os | functions: main"):
    return {
        "cache_key": cache_key,
        "file_path": file_path,
        "file_hash": "deadbeef",
        "summary": summary,
        "summary_type": summary_type,
        "language": "python",
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "dev@example.com",
        "file_size_bytes": 100,
        "schema_version": "v1",
    }


def test_insert_and_get_summary(conn):
    obj = _summary()
    insert_summary(conn, obj)
    result = get_summary(conn, "abc123")
    assert result is not None
    assert result["file_path"] == "src/foo.py"
    assert result["summary_type"] == "static"


def test_ai_summary_not_overwritten_by_static(conn):
    ai = _summary(summary_type="ai", summary="Rich AI summary")
    insert_summary(conn, ai)
    static = _summary(summary_type="static", summary="Static summary")
    insert_summary(conn, static)
    result = get_summary(conn, "abc123")
    assert result["summary_type"] == "ai"
    assert result["summary"] == "Rich AI summary"


def test_get_summary_by_path_prefers_ai(conn):
    insert_summary(conn, _summary(cache_key="k1", summary_type="static"))
    insert_summary(conn, _summary(cache_key="k2", summary_type="ai", summary="AI version"))
    result = get_summary_by_path(conn, "src/foo.py")
    assert result["summary_type"] == "ai"


def test_coverage_stats(conn):
    insert_summary(conn, _summary(cache_key="k1", file_path="a.py", summary_type="ai"))
    insert_summary(conn, _summary(cache_key="k2", file_path="b.py", summary_type="static"))
    stats = coverage_stats(conn)
    assert stats["ai"] == 1
    assert stats["static"] == 1
    assert stats["indexed"] == 2


def test_keyword_search(conn):
    insert_summary(conn, _summary(cache_key="k1", file_path="auth.py", summary="classes: AuthMiddleware | functions: validate_token"))
    insert_summary(conn, _summary(cache_key="k2", file_path="db.py", summary="imports: sqlite3 | functions: connect"))
    results = keyword_search(conn, "AuthMiddleware")
    assert any(r["file_path"] == "auth.py" for r in results)


def test_invalidate_by_path(conn):
    insert_summary(conn, _summary())
    invalidate_by_path(conn, "src/foo.py")
    assert get_summary(conn, "abc123") is None


def test_top_contributors(conn):
    insert_summary(conn, _summary(cache_key="k1", file_path="a.py", summary_type="ai"))
    insert_summary(conn, _summary(cache_key="k2", file_path="b.py", summary_type="ai"))
    contribs = top_contributors(conn)
    assert contribs[0]["email"] == "dev@example.com"
    assert contribs[0]["count"] == 2


def test_static_only_files(conn):
    insert_summary(conn, _summary(cache_key="k1", file_path="a.py", summary_type="static"))
    insert_summary(conn, _summary(cache_key="k2", file_path="b.py", summary_type="ai"))
    only = static_only_files(conn)
    assert "a.py" in only
    assert "b.py" not in only


def test_insert_symbol_and_find(conn):
    obj = {
        "cache_key": "sym1",
        "file_path": "src/service.py",
        "file_hash": "deadbeef",
        "language": "python",
        "symbols": {
            "functions": [{"name": "UserService", "line": 10, "signature": "def UserService():"}],
            "classes": [],
            "imports": [],
            "exports": [],
        },
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "v1",
    }
    insert_symbol(conn, obj)
    results = find_symbol_by_name(conn, "UserService")
    assert len(results) == 1
    assert results[0]["file_path"] == "src/service.py"
