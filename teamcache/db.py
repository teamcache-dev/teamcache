from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .search import normalize_fts_query


MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "ALTER TABLE summaries ADD COLUMN last_accessed_at TEXT", "add last_accessed_at column"),
    (2, "ALTER TABLE summaries ADD COLUMN git_commit TEXT", "add git_commit column"),
    (3, """CREATE TABLE IF NOT EXISTS daily_stats (
      date TEXT PRIMARY KEY,
      ai_count INTEGER DEFAULT 0,
      static_count INTEGER DEFAULT 0,
      hit_count INTEGER DEFAULT 0,
      miss_count INTEGER DEFAULT 0
  )""", "add daily_stats table"),
    (4, """CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_type TEXT NOT NULL,
      cache_key TEXT,
      file_path TEXT,
      created_by TEXT,
      summary_type TEXT,
      occurred_at TEXT NOT NULL
  )""", "add audit_log table"),
    (5, "ALTER TABLE summaries ADD COLUMN quality_score REAL DEFAULT 0.5", "add quality_score column"),
]


def _run_migrations(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    ran: list[int] = []
    for version, sql, _description in MIGRATIONS:
        if version in applied:
            continue
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()
        ran.append(version)
    return ran


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            cache_key TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            summary_type TEXT NOT NULL,
            language TEXT,
            created_at TEXT,
            created_by TEXT,
            file_size_bytes INTEGER,
            schema_version TEXT
        )
        """
    )
    existing_fts = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fts_summaries'"
    ).fetchone()
    if existing_fts:
        fts_sql = existing_fts["sql"] or ""
        if "content=" in fts_sql.lower() or "language" not in fts_sql:
            conn.execute("DROP TABLE fts_summaries")
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_summaries USING fts5(
            file_path,
            summary,
            language,
            tokenize='unicode61'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_file_path ON summaries(file_path)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            cache_key TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            language TEXT,
            symbols_json TEXT NOT NULL,
            created_at TEXT,
            schema_version TEXT
        )
        """
    )
    existing_symbol_fts = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fts_symbols'"
    ).fetchone()
    if existing_symbol_fts:
        fts_sql = existing_symbol_fts["sql"] or ""
        if "symbol_names" not in fts_sql:
            conn.execute("DROP TABLE fts_symbols")
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
            file_path,
            symbol_names,
            tokenize='unicode61'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path)"
    )
    conn.commit()
    _run_migrations(conn)
    return conn


def compute_quality_score(summary: str, file_size_bytes: int) -> float:
    words = summary.split()
    word_count = len(words)
    length_score = min(word_count / 100, 1.0)
    cap_ratio = sum(1 for w in words if w and w[0].isupper()) / max(word_count, 1)
    specificity_score = min(cap_ratio * 2, 1.0)
    return round(0.5 * length_score + 0.5 * specificity_score, 3)


def insert_summary(conn: sqlite3.Connection, obj: dict[str, Any], commit: bool = True) -> None:
    existing = get_summary(conn, obj["cache_key"])
    if existing and existing["summary_type"] == "ai" and obj["summary_type"] == "static":
        return

    quality_score = compute_quality_score(obj["summary"], obj.get("file_size_bytes") or 0)
    conn.execute(
        """
        INSERT INTO summaries (
            cache_key,
            file_path,
            file_hash,
            summary,
            summary_type,
            language,
            created_at,
            created_by,
            file_size_bytes,
            schema_version,
            last_accessed_at,
            quality_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            file_path = excluded.file_path,
            file_hash = excluded.file_hash,
            summary = excluded.summary,
            summary_type = excluded.summary_type,
            language = excluded.language,
            created_at = excluded.created_at,
            created_by = excluded.created_by,
            file_size_bytes = excluded.file_size_bytes,
            schema_version = excluded.schema_version,
            quality_score = excluded.quality_score
        """,
        (
            obj["cache_key"],
            obj["file_path"],
            obj["file_hash"],
            obj["summary"],
            obj["summary_type"],
            obj.get("language"),
            obj.get("created_at"),
            obj.get("created_by"),
            obj.get("file_size_bytes"),
            obj.get("schema_version"),
            quality_score,
        ),
    )
    row = conn.execute(
        "SELECT rowid FROM summaries WHERE cache_key = ?",
        (obj["cache_key"],),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM fts_summaries WHERE rowid = ?", (row["rowid"],))
        conn.execute(
            """
            INSERT INTO fts_summaries(rowid, file_path, summary, language)
            VALUES (?, ?, ?, ?)
            """,
            (
                row["rowid"],
                obj["file_path"],
                obj["summary"],
                obj.get("language"),
            ),
        )
    if commit:
        conn.commit()


def get_summary(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE summaries SET last_accessed_at = ? WHERE cache_key = ?",
            (datetime.utcnow().isoformat() + "Z", row["cache_key"]),
        )
        conn.commit()
        return dict(row)
    return None


def get_summary_by_path(conn: sqlite3.Connection, file_path: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM summaries
        WHERE file_path = ?
        ORDER BY CASE WHEN summary_type = 'ai' THEN 0 ELSE 1 END, created_at DESC
        LIMIT 1
        """,
        (file_path,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE summaries SET last_accessed_at = ? WHERE cache_key = ?",
            (datetime.utcnow().isoformat() + "Z", row["cache_key"]),
        )
        conn.commit()
        return dict(row)
    return None


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    normalized_query = normalize_fts_query(query)
    if not normalized_query:
        return []
    rows = conn.execute(
        """
        SELECT
            s.file_path,
            s.summary,
            s.summary_type,
            s.language,
            bm25(fts_summaries) AS rank
        FROM fts_summaries
        JOIN summaries s ON s.rowid = fts_summaries.rowid
        WHERE fts_summaries MATCH ?
        ORDER BY CASE WHEN s.summary_type = 'ai' THEN 0 ELSE 1 END, rank ASC
        LIMIT ?
        """,
        (normalized_query, limit),
    ).fetchall()
    return [
        {
            "file_path": row["file_path"],
            "summary": row["summary"],
            "summary_type": row["summary_type"],
            "language": row["language"],
        }
        for row in rows
    ]


def rebuild_from_objects(conn: sqlite3.Connection, summaries_root: Path) -> None:
    if not summaries_root.exists():
        return
    for path in summaries_root.glob("**/*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            insert_summary(conn, obj, commit=False)
        except Exception as exc:  # noqa: BLE001 - malformed cache files should not abort startup.
            print(f"warning: failed to parse summary object {path}: {exc}", file=sys.stderr)
    conn.commit()


def optimize_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO fts_summaries(fts_summaries) VALUES('optimize')")
    conn.execute("INSERT INTO fts_symbols(fts_symbols) VALUES('optimize')")
    conn.commit()


def count_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT summary_type, COUNT(*) AS count FROM summaries GROUP BY summary_type"
    ).fetchall()
    counts = {"ai": 0, "static": 0, "total": 0}
    for row in rows:
        if row["summary_type"] in {"ai", "static"}:
            counts[row["summary_type"]] = row["count"]
        counts["total"] += row["count"]
    return counts


def insert_symbol(conn: sqlite3.Connection, obj: dict[str, Any], commit: bool = True) -> None:
    symbols_json = json.dumps(obj.get("symbols", {}), sort_keys=True)
    conn.execute(
        """
        INSERT INTO symbols (
            cache_key,
            file_path,
            file_hash,
            language,
            symbols_json,
            created_at,
            schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            file_path = excluded.file_path,
            file_hash = excluded.file_hash,
            language = excluded.language,
            symbols_json = excluded.symbols_json,
            created_at = excluded.created_at,
            schema_version = excluded.schema_version
        """,
        (
            obj["cache_key"],
            obj["file_path"],
            obj["file_hash"],
            obj.get("language"),
            symbols_json,
            obj.get("created_at"),
            obj.get("schema_version"),
        ),
    )
    row = conn.execute(
        "SELECT rowid FROM symbols WHERE cache_key = ?",
        (obj["cache_key"],),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM fts_symbols WHERE rowid = ?", (row["rowid"],))
        conn.execute(
            """
            INSERT INTO fts_symbols(rowid, file_path, symbol_names)
            VALUES (?, ?, ?)
            """,
            (
                row["rowid"],
                obj["file_path"],
                _symbol_names(obj.get("symbols", {})),
            ),
        )
    if commit:
        conn.commit()


def get_symbol(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM symbols WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    return _symbol_row_to_dict(row) if row else None


def get_symbol_by_path(conn: sqlite3.Connection, file_path: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM symbols
        WHERE file_path = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (file_path,),
    ).fetchone()
    return _symbol_row_to_dict(row) if row else None


def find_symbol_by_name(
    conn: sqlite3.Connection,
    symbol_name: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    normalized_query = normalize_fts_query(symbol_name)
    if not normalized_query:
        return []
    rows = conn.execute(
        """
        SELECT
            s.cache_key,
            s.file_path,
            s.language,
            fts_symbols.symbol_names,
            bm25(fts_symbols) AS rank
        FROM fts_symbols
        JOIN symbols s ON s.rowid = fts_symbols.rowid
        WHERE fts_symbols MATCH ?
        ORDER BY rank ASC
        LIMIT ?
        """,
        (normalized_query, limit),
    ).fetchall()
    return [
        {
            "cache_key": row["cache_key"],
            "file_path": row["file_path"],
            "language": row["language"],
            "symbol_names": row["symbol_names"],
        }
        for row in rows
    ]


def rebuild_symbols(conn: sqlite3.Connection, symbols_root: Path) -> int:
    if not symbols_root.exists():
        return 0
    count = 0
    for path in symbols_root.glob("**/*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            insert_symbol(conn, obj, commit=False)
            count += 1
        except Exception as exc:  # noqa: BLE001 - malformed cache files should not abort sync.
            print(f"warning: failed to parse symbol object {path}: {exc}", file=sys.stderr)
    conn.commit()
    return count


def invalidate_by_path(conn: sqlite3.Connection, file_path: str) -> None:
    summary_rows = conn.execute(
        """
        SELECT rowid, cache_key, file_path, created_by, summary_type
        FROM summaries
        WHERE file_path = ?
        """,
        (file_path,),
    ).fetchall()
    symbol_rows = conn.execute(
        "SELECT rowid FROM symbols WHERE file_path = ?",
        (file_path,),
    ).fetchall()
    for row in summary_rows:
        write_audit_event(
            conn,
            "evict",
            row["cache_key"],
            row["file_path"],
            row["created_by"],
            row["summary_type"],
            commit=False,
        )
        conn.execute("DELETE FROM fts_summaries WHERE rowid = ?", (row["rowid"],))
    for row in symbol_rows:
        conn.execute("DELETE FROM fts_symbols WHERE rowid = ?", (row["rowid"],))
    conn.execute("DELETE FROM summaries WHERE file_path = ?", (file_path,))
    conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
    conn.commit()


def invalidate_stale(conn: sqlite3.Connection, days: int = 30) -> int:
    summary_rows = conn.execute(
        """
        SELECT rowid, cache_key, file_path, created_by, summary_type FROM summaries
        WHERE (julianday('now') - julianday(COALESCE(last_accessed_at, created_at))) > ?
        """,
        (days,),
    ).fetchall()
    with conn:
        for row in summary_rows:
            write_audit_event(
                conn,
                "evict",
                row["cache_key"],
                row["file_path"],
                row["created_by"],
                row["summary_type"],
                commit=False,
            )
            conn.execute("DELETE FROM fts_summaries WHERE rowid = ?", (row["rowid"],))
            symbol_row = conn.execute(
                "SELECT rowid FROM symbols WHERE cache_key = ?",
                (row["cache_key"],),
            ).fetchone()
            if symbol_row:
                conn.execute("DELETE FROM fts_symbols WHERE rowid = ?", (symbol_row["rowid"],))
                conn.execute("DELETE FROM symbols WHERE cache_key = ?", (row["cache_key"],))
            conn.execute("DELETE FROM summaries WHERE rowid = ?", (row["rowid"],))

        orphan_symbol_rows = conn.execute(
            """
            SELECT symbols.rowid
            FROM symbols
            LEFT JOIN summaries ON summaries.cache_key = symbols.cache_key
            WHERE summaries.cache_key IS NULL
            """
        ).fetchall()
        for row in orphan_symbol_rows:
            conn.execute("DELETE FROM fts_symbols WHERE rowid = ?", (row["rowid"],))
            conn.execute("DELETE FROM symbols WHERE rowid = ?", (row["rowid"],))
    return len(summary_rows)


def invalidate_all(conn: sqlite3.Connection) -> None:
    summary_rows = conn.execute(
        "SELECT cache_key, file_path, created_by, summary_type FROM summaries"
    ).fetchall()
    with conn:
        for row in summary_rows:
            write_audit_event(
                conn,
                "evict",
                row["cache_key"],
                row["file_path"],
                row["created_by"],
                row["summary_type"],
                commit=False,
            )
        conn.execute("DELETE FROM fts_summaries")
        conn.execute("DELETE FROM fts_symbols")
        conn.execute("DELETE FROM summaries")
        conn.execute("DELETE FROM symbols")


def summary_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]


def symbol_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]


def coverage_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT file_path,
               MAX(CASE WHEN summary_type = 'ai' THEN 1 ELSE 0 END) AS has_ai
        FROM summaries
        GROUP BY file_path
        """
    ).fetchall()
    ai = sum(1 for r in rows if r["has_ai"])
    return {"ai": ai, "static": len(rows) - ai, "indexed": len(rows)}


def top_contributors(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT created_by, COUNT(*) AS count
        FROM summaries
        WHERE summary_type = 'ai'
          AND created_by IS NOT NULL
          AND created_by != ''
        GROUP BY created_by
        ORDER BY count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"email": row["created_by"], "count": row["count"]} for row in rows]


def static_only_files(conn: sqlite3.Connection, limit: int = 5) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT s.file_path
        FROM summaries s
        WHERE s.summary_type = 'static'
          AND NOT EXISTS (
              SELECT 1 FROM summaries s2
              WHERE s2.file_path = s.file_path AND s2.summary_type = 'ai'
          )
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["file_path"] for row in rows]


def record_cache_event(conn: sqlite3.Connection, event_type: str) -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (today,))
    if event_type == "hit":
        conn.execute(
            "UPDATE daily_stats SET hit_count = hit_count + 1 WHERE date = ?", (today,)
        )
    else:
        conn.execute(
            "UPDATE daily_stats SET miss_count = miss_count + 1 WHERE date = ?", (today,)
        )
    conn.commit()


def write_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    cache_key: str | None,
    file_path: str | None,
    created_by: str | None,
    summary_type: str | None,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (
            event_type,
            cache_key,
            file_path,
            created_by,
            summary_type,
            occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            cache_key,
            file_path,
            created_by,
            summary_type,
            datetime.utcnow().isoformat() + "Z",
        ),
    )
    if commit:
        conn.commit()


def metrics_summary(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    cov = coverage_stats(conn)
    total_files = cov["indexed"]
    ai_coverage_pct = round(cov["ai"] / total_files * 100, 1) if total_files else 0.0
    static_coverage_pct = round(cov["static"] / total_files * 100, 1) if total_files else 0.0

    stale_cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat() + "Z"
    stale_count = conn.execute(
        """
        SELECT COUNT(*) FROM summaries
        WHERE (last_accessed_at IS NULL OR last_accessed_at < ?)
        """,
        (stale_cutoff,),
    ).fetchone()[0]

    est_tokens_row = conn.execute(
        """
        SELECT SUM(file_size_bytes) FROM summaries
        WHERE summary_type = 'ai' AND file_size_bytes IS NOT NULL
        """
    ).fetchone()
    estimated_tokens_saved = int((est_tokens_row[0] or 0) * 0.25)

    daily_query = """
        SELECT date, hit_count, miss_count
        FROM daily_stats
        ORDER BY date DESC
        LIMIT 30
    """
    daily_hits = [
        {"date": row["date"], "hit_count": row["hit_count"], "miss_count": row["miss_count"]}
        for row in conn.execute(daily_query).fetchall()
    ]

    return {
        "ai_coverage_pct": ai_coverage_pct,
        "static_coverage_pct": static_coverage_pct,
        "total_files": total_files,
        "top_contributors": top_contributors(conn),
        "stale_count": stale_count,
        "estimated_tokens_saved": estimated_tokens_saved,
        "daily_hits": daily_hits,
    }


def _symbol_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["symbols"] = json.loads(result.pop("symbols_json") or "{}")
    return result


def _symbol_names(symbols: dict[str, Any]) -> str:
    names: list[str] = []
    for value in symbols.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    name = item.get("name")
                    if name:
                        names.append(str(name))
                    methods = item.get("methods")
                    if isinstance(methods, list):
                        for method in methods:
                            if isinstance(method, dict):
                                method_name = method.get("name")
                                if method_name:
                                    names.append(str(method_name))
                            elif method:
                                names.append(str(method))
                elif item:
                    names.append(str(item))
    return " ".join(names)
