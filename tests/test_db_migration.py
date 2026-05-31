from __future__ import annotations

import sqlite3 as sqlite3_stdlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from teamcache.db import connect, get_summary, insert_summary, invalidate_stale


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "index.sqlite"


@pytest.fixture()
def conn(db_path: Path):
    c = connect(db_path)
    yield c
    c.close()


def _summary(
    cache_key: str = "abc123",
    file_path: str = "src/foo.py",
    summary_type: str = "static",
    summary: str = "imports: os | functions: main",
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "cache_key": cache_key,
        "file_path": file_path,
        "file_hash": "deadbeef",
        "summary": summary,
        "summary_type": summary_type,
        "language": "python",
        "created_at": created_at,
        "created_by": "dev@example.com",
        "file_size_bytes": 100,
        "schema_version": "v2",
    }


def _old_timestamp(days_ago: int = 60) -> str:
    dt = datetime.utcnow() - timedelta(days=days_ago)
    return dt.isoformat() + "Z"


def _recent_timestamp(days_ago: int = 1) -> str:
    dt = datetime.utcnow() - timedelta(days=days_ago)
    return dt.isoformat() + "Z"


# ---------------------------------------------------------------------------
# 1. Migration adds last_accessed_at column to summaries table
# ---------------------------------------------------------------------------

def test_migration_adds_last_accessed_at_column(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(summaries)")
    }
    assert "last_accessed_at" in columns


# ---------------------------------------------------------------------------
# 2. Calling connect twice on the same DB path does not raise
# ---------------------------------------------------------------------------

def test_migration_is_idempotent(db_path: Path) -> None:
    c1 = connect(db_path)
    c1.close()
    # Second connect must apply the migration without raising an error
    c2 = connect(db_path)
    c2.close()


# ---------------------------------------------------------------------------
# 3. invalidate_stale keeps rows with old created_at but recent last_accessed_at
# ---------------------------------------------------------------------------

def test_invalidate_stale_uses_last_accessed(conn) -> None:
    old_created = _old_timestamp(days_ago=60)
    recent_accessed = _recent_timestamp(days_ago=1)

    obj = _summary(cache_key="keep_me", created_at=old_created)
    insert_summary(conn, obj)

    # Manually set last_accessed_at to a recent timestamp so the row looks
    # actively used even though created_at is old.
    conn.execute(
        "UPDATE summaries SET last_accessed_at = ? WHERE cache_key = ?",
        (recent_accessed, "keep_me"),
    )
    conn.commit()

    evicted = invalidate_stale(conn, days=30)

    assert evicted == 0
    result = get_summary(conn, "keep_me")
    assert result is not None
    assert result["cache_key"] == "keep_me"


# ---------------------------------------------------------------------------
# 4. invalidate_stale evicts rows with old created_at and old last_accessed_at
# ---------------------------------------------------------------------------

def test_invalidate_stale_evicts_old_unaccessed(conn) -> None:
    old_created = _old_timestamp(days_ago=60)
    old_accessed = _old_timestamp(days_ago=45)

    obj = _summary(cache_key="evict_me", created_at=old_created)
    insert_summary(conn, obj)

    # Set last_accessed_at to an old timestamp — row should be evicted.
    conn.execute(
        "UPDATE summaries SET last_accessed_at = ? WHERE cache_key = ?",
        (old_accessed, "evict_me"),
    )
    conn.commit()

    evicted = invalidate_stale(conn, days=30)

    assert evicted >= 1
    result = get_summary(conn, "evict_me")
    assert result is None
