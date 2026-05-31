from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_MODEL: Any | None = None
_MODEL_LOAD_FAILED = False


def connect_embeddings(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            cache_key TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            embedding BLOB NOT NULL,
            summary_type TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _model_enabled(config: Any = None) -> bool:
    """Return True only if embeddings are explicitly enabled in config.

    Defaults to False so that the first-run experience never triggers an
    unexpected ~80 MB download.
    """
    if config is None:
        return False
    return bool(getattr(config, "enable_embeddings", False))


def embeddings_count(conn: sqlite3.Connection) -> int:
    """Return the number of stored embeddings, or 0 on any error."""
    try:
        return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


def should_use_embeddings(emb_conn: sqlite3.Connection, config: Any) -> bool:
    """Return True when embeddings are enabled and below the row-count guard."""
    if not getattr(config, "enable_embeddings", False):
        return False
    count = embeddings_count(emb_conn)
    limit = getattr(config, "max_files_for_local_embeddings", 5000)
    return count <= limit


def get_model(config: Any = None) -> Any | None:
    global _MODEL, _MODEL_LOAD_FAILED
    # P1-D: return None immediately when embeddings are disabled (default).
    if not _model_enabled(config):
        return None
    if _MODEL is not None:
        return _MODEL
    if _MODEL_LOAD_FAILED:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        import sys
        from pathlib import Path

        # Check whether the model is already cached locally (~/.cache/huggingface).
        # If not, warn once that an ~80MB download is about to happen.
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        model_cached = any(
            d.name.startswith("models--sentence-transformers--all-MiniLM-L6-v2")
            for d in cache_dir.iterdir()
        ) if cache_dir.is_dir() else False
        if not model_cached:
            print(
                "teamcache: downloading embedding model all-MiniLM-L6-v2 (~80MB, one-time)...",
                file=sys.stderr,
            )
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _MODEL
    except Exception:  # noqa: BLE001 - embeddings are optional.
        _MODEL = None
        _MODEL_LOAD_FAILED = True
        return None


def upsert_embedding(
    conn: sqlite3.Connection,
    cache_key: str,
    file_path: str,
    summary: str,
    summary_type: str,
    config: Any = None,
) -> None:
    model = get_model(config)
    if model is None:
        return
    try:
        import numpy as np

        embedding = np.asarray(model.encode(summary), dtype=np.float32)
        conn.execute(
            """
            INSERT OR REPLACE INTO embeddings (
                cache_key,
                file_path,
                embedding,
                summary_type,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                file_path,
                embedding.tobytes(),
                summary_type,
                datetime.utcnow().isoformat() + "Z",
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - embedding failure should not block indexing.
        return


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 5,
    config: Any = None,
) -> list[dict[str, Any]]:
    model = get_model(config)
    if model is None:
        return []
    try:
        import numpy as np

        rows = conn.execute(
            "SELECT cache_key, file_path, embedding, summary_type FROM embeddings"
        ).fetchall()
        if not rows:
            return []
        query_vec = np.asarray(model.encode(query), dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm == 0:
            return []
        scored: list[dict[str, Any]] = []
        for row in rows:
            emb = np.frombuffer(row["embedding"], dtype=np.float32)
            emb_norm = float(np.linalg.norm(emb))
            if emb_norm == 0 or emb.shape != query_vec.shape:
                continue
            score = float(np.dot(query_vec, emb) / (query_norm * emb_norm))
            scored.append(
                {
                    "file_path": row["file_path"],
                    "cache_key": row["cache_key"],
                    "summary_type": row["summary_type"],
                    "score": score,
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]
    except Exception:  # noqa: BLE001
        return []
