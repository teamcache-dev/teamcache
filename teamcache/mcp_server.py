from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import find_repo_root, load_config
from .constants import (
    BINARY_EXTENSIONS,
    EMBEDDINGS_DB,
    ENTRY_POINTS,
    INDEX_DB,
    LANGUAGE_BY_SUFFIX,
    SKIP_DIRS,
    SKIP_SUFFIXES,
    SUMMARIES_DIR,
    SYMBOLS_DIR,
)
from .db import (
    connect,
    count_by_type,
    find_symbol_by_name,
    get_summary_by_path,
    get_symbol,
    get_symbol_by_path,
    get_summary,
    insert_summary,
    keyword_search,
    optimize_fts,
    rebuild_from_objects,
    rebuild_symbols,
)
from .embeddings import connect_embeddings, semantic_search, upsert_embedding
from .files import cache_key, file_hash, git_user_email, write_summary_object
from .secrets import contains_secret


def run_server() -> None:
    repo_root = find_repo_root(Path.cwd())
    repo_resolved = repo_root.resolve()
    config = load_config(repo_root)
    conn = connect(repo_root / INDEX_DB)
    emb_conn = connect_embeddings(repo_root / EMBEDDINGS_DB)
    summaries_root = repo_root / SUMMARIES_DIR
    symbols_root = repo_root / SYMBOLS_DIR
    db_row_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    if db_row_count == 0:
        rebuild_from_objects(conn, summaries_root)
        rebuild_symbols(conn, symbols_root)
        optimize_fts(conn)

    mcp = FastMCP("teamcache")

    @mcp.tool()
    def repo_overview() -> str | dict[str, str]:
        try:
            return _repo_overview(repo_root, conn)
        except Exception as exc:  # noqa: BLE001 - MCP tools must report errors as data.
            return {"error": str(exc)}

    @mcp.tool()
    def get_file_context(file_path: str) -> dict[str, Any]:
        try:
            rel, path = _sanitize_path(repo_root, repo_resolved, file_path)
            if rel is None or path is None:
                return {"error": "path outside repository"}

            language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown")
            obj = None
            if path.is_file():
                try:
                    content = path.read_bytes()
                    fhash = file_hash(content)
                    key = cache_key(fhash, config.schema_version)
                    obj = get_summary(conn, key)
                except OSError:
                    pass
            if obj and obj["summary_type"] == "ai":
                return {
                    "summary": obj["summary"],
                    "summary_type": "ai",
                    "cached": True,
                    "file_path": file_path,
                    "language": language,
                    "note": "Read exact source before editing.",
                }
            if obj and obj["summary_type"] == "static":
                return {
                    "summary": obj["summary"],
                    "summary_type": "static",
                    "cached": True,
                    "file_path": file_path,
                    "language": language,
                    "note": "Static index only. Read file for full understanding, then call cache_summary().",
                }
            return {
                "summary": None,
                "summary_type": None,
                "cached": False,
                "file_path": file_path,
                "language": language,
                "note": "Not indexed. Read file normally, then call cache_summary().",
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @mcp.tool()
    def cache_summary(file_path: str, summary: str, language: str) -> dict[str, Any]:
        try:
            rel, path = _sanitize_path(repo_root, repo_resolved, file_path)
            if rel is None or path is None:
                return {"stored": False, "error": "path outside repository"}
            if not path.exists():
                return {"stored": False, "error": "file not found"}
            if not summary or not summary.strip():
                return {"stored": False, "error": "summary must not be empty"}
            if contains_secret(summary):
                return {"stored": False, "error": "summary appears to contain a secret or credential — not cached"}

            content = path.read_bytes()
            fhash = file_hash(content)
            key = cache_key(fhash, config.schema_version)
            existing = get_summary(conn, key)
            if existing and existing["summary_type"] == "ai":
                return {
                    "stored": False,
                    "reason": "ai summary already cached",
                    "cache_key": key,
                }

            obj = {
                "cache_key": key,
                "file_path": rel,
                "file_hash": fhash,
                "summary": summary.strip(),
                "summary_type": "ai",
                "schema_version": config.schema_version,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "created_by": git_user_email(repo_root),
                "file_size_bytes": len(content),
                "language": language,
            }
            write_summary_object(summaries_root, key, obj)
            insert_summary(conn, obj)
            upsert_embedding(emb_conn, key, rel, summary.strip(), "ai")
            return {"stored": True, "cache_key": key, "summary_type": "ai"}
        except Exception as exc:  # noqa: BLE001
            return {"stored": False, "error": str(exc)}

    @mcp.tool()
    def find_relevant_files(task: str) -> list[dict[str, Any]]:
        try:
            if not task or not task.strip():
                return []
            semantic = _semantic_results(conn, emb_conn, task, limit=5)
            keyword = keyword_search(conn, task, limit=5)
            return _merge_relevance_results(semantic, keyword, limit=5)
        except Exception as exc:  # noqa: BLE001 - MCP tools must report errors as data.
            import sys
            print(f"warning: find_relevant_files failed: {exc}", file=sys.stderr)
            return [{"error": str(exc)}]

    @mcp.tool()
    def get_symbols(file_path: str) -> dict[str, Any]:
        try:
            rel, path = _sanitize_path(repo_root, repo_resolved, file_path)
            if rel is None or path is None:
                return {"error": "path outside repository"}
            obj = get_symbol_by_path(conn, rel)
            if not obj or not path.is_file():
                return {
                    "symbols": None,
                    "cached": False,
                    "note": "No symbol index. Run teamcache index or read the file.",
                }
            try:
                if file_hash(path.read_bytes()) != obj["file_hash"]:
                    return {
                        "symbols": None,
                        "cached": False,
                        "note": "No symbol index. Run teamcache index or read the file.",
                    }
            except OSError:
                return {
                    "symbols": None,
                    "cached": False,
                    "note": "No symbol index. Run teamcache index or read the file.",
                }
            return {
                "symbols": obj["symbols"],
                "cached": True,
                "file_path": file_path,
                "language": obj["language"],
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @mcp.tool()
    def find_by_symbol(symbol_name: str) -> list[dict[str, Any]]:
        try:
            if not symbol_name or not symbol_name.strip():
                return []
            results = []
            for result in find_symbol_by_name(conn, symbol_name, limit=10):
                obj = get_symbol(conn, result["cache_key"])
                results.append(
                    {
                        "file_path": result["file_path"],
                        "language": result["language"],
                        "line": _symbol_line(obj["symbols"] if obj else {}, symbol_name),
                        "symbol_name": symbol_name,
                    }
                )
            return results
        except Exception as exc:  # noqa: BLE001 - MCP tools must report errors as data.
            import sys
            print(f"warning: find_by_symbol failed: {exc}", file=sys.stderr)
            return [{"error": str(exc)}]

    @mcp.tool()
    def get_changed_context(since_branch: str = "main") -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{since_branch}...HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return {"error": "git diff failed", "since_branch": since_branch}
        if result.returncode != 0:
            return {"error": "git diff failed", "since_branch": since_branch}

        changed_files = []
        files_needing_review = []
        ai_count = 0
        for rel in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            obj = get_summary_by_path(conn, rel)
            summary_type = obj["summary_type"] if obj else None
            has_ai_summary = summary_type == "ai"
            has_static_summary = summary_type == "static"
            if has_ai_summary:
                ai_count += 1
            if summary_type is None or summary_type == "static":
                files_needing_review.append(rel)
            changed_files.append(
                {
                    "file_path": rel,
                    "summary_type": summary_type,
                    "has_ai_summary": has_ai_summary,
                    "has_static_summary": has_static_summary,
                }
            )
        total = len(changed_files)
        return {
            "since_branch": since_branch,
            "changed_files": changed_files,
            "files_needing_review": files_needing_review,
            "ai_coverage": f"{ai_count} of {total} changed files have AI summaries",
        }

    try:
        mcp.run(transport="stdio")
    finally:
        conn.close()
        emb_conn.close()


def _repo_overview(repo_root: Path, conn: Any) -> str:
    tree, language_counts = _repo_scan(repo_root)
    total_on_disk = sum(language_counts.values())
    languages = "\n".join(
        f"- {suffix}: {count}"
        for suffix, count in sorted(language_counts.items(), key=lambda item: item[1], reverse=True)
    )
    coverage = count_by_type(conn)
    entry_points = sorted(
        entry for entry in ENTRY_POINTS if (repo_root / entry).exists()
    )
    entry_text = "\n".join(f"- {entry}" for entry in entry_points) or "- none found"

    return (
        "## Directory Structure\n"
        f"{tree}\n\n"
        "## Languages\n"
        f"{languages or '- none found'}\n\n"
        "## Summary Coverage\n"
        f"{coverage['ai']} ai, {coverage['static']} static, {coverage['total']} indexed of {total_on_disk} total files\n\n"
        "## Entry Points\n"
        f"{entry_text}"
    )


def _repo_scan(repo_root: Path) -> tuple[str, dict[str, int]]:
    lines = [repo_root.name + "/"]
    language_counts: dict[str, int] = {}
    root_depth = len(repo_root.parts)
    for dirpath, dirs, files in os.walk(repo_root):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        dirs[:] = sorted(dirname for dirname in dirs if dirname not in SKIP_DIRS)
        visible_files = sorted(files)
        indent = "  " * (depth + 1)
        if depth < 2:
            for dirname in dirs:
                lines.append(f"{indent}{dirname}/")
        for filename in visible_files:
            suffix = Path(filename).suffix.lower()
            if suffix in SKIP_SUFFIXES or suffix in BINARY_EXTENSIONS:
                continue
            lang = LANGUAGE_BY_SUFFIX.get(suffix, suffix or "<none>")
            language_counts[lang] = language_counts.get(lang, 0) + 1
            if depth <= 2:
                lines.append(f"{indent}{filename}")
    return "\n".join(lines), language_counts


def _semantic_results(
    conn: Any,
    emb_conn: Any,
    task: str,
    limit: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in semantic_search(emb_conn, task, limit=limit):
        obj = get_summary(conn, row["cache_key"])
        if obj:
            results.append(
                {
                    "file_path": obj["file_path"],
                    "summary": obj["summary"],
                    "summary_type": obj["summary_type"],
                    "language": obj["language"],
                    "score": row["score"],
                }
            )
    return results


def _merge_relevance_results(
    semantic: list[dict[str, Any]],
    keyword: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    max_len = max(len(semantic), len(keyword))
    for index in range(max_len):
        if index < len(semantic):
            ordered.append(semantic[index])
        if index < len(keyword):
            ordered.append(keyword[index])
    for item in ordered:
        existing = by_path.get(item["file_path"])
        if existing is None:
            by_path[item["file_path"]] = item
        elif existing.get("summary_type") != "ai" and item.get("summary_type") == "ai":
            by_path[item["file_path"]] = item
    return list(by_path.values())[:limit]


def _symbol_line(symbols: dict[str, Any], symbol_name: str) -> int | None:
    for key in ("functions", "classes", "types"):
        for item in symbols.get(key, []):
            if not isinstance(item, dict):
                continue
            if item.get("name") == symbol_name:
                return item.get("line")
            for method in item.get("methods", []):
                if isinstance(method, dict) and method.get("name") == symbol_name:
                    return method.get("line")
                # legacy: methods stored as plain strings — fall back to class line
                if isinstance(method, str) and method == symbol_name:
                    return item.get("line")
    return None


def _sanitize_path(
    repo_root: Path,
    repo_resolved: Path,
    file_path: str,
) -> tuple[str | None, Path | None]:
    rel = file_path.replace("\\", "/").lstrip("/")
    if not rel or rel == ".":
        return None, None
    path = (repo_root / rel).resolve()
    if not path.is_relative_to(repo_resolved):
        return None, None
    return path.relative_to(repo_resolved).as_posix(), path
