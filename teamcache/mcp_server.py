from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
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
    compute_quality_score,
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
    write_audit_event,
)
from .embeddings import connect_embeddings, semantic_search, upsert_embedding
from .files import cache_key, file_hash, git_user_email, is_sensitive_path
from .store import make_store
from .secrets import contains_secret

MAX_SUMMARY_BYTES = 8192
MIN_SUMMARY_WORDS = 5

_WRITTEN_THIS_SESSION: set[str] = set()
_OVERVIEW_CACHE: dict[str, Any] = {}
_GIT_MTIME_CACHE: dict[str, str | None] = {}


def run_server() -> None:
    repo_root = find_repo_root(Path.cwd())
    repo_resolved = repo_root.resolve()
    config = load_config(repo_root)
    store = make_store(config, repo_root)
    # WAL mode supports concurrent readers alongside one writer; use separate
    # connections so reads never block on an in-progress write transaction.
    read_conn = connect(repo_root / INDEX_DB)
    write_conn = connect(repo_root / INDEX_DB)
    emb_conn = connect_embeddings(repo_root / EMBEDDINGS_DB)
    summaries_root = repo_root / SUMMARIES_DIR
    symbols_root = repo_root / SYMBOLS_DIR
    db_row_count = read_conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    if db_row_count == 0:
        rebuild_from_objects(write_conn, summaries_root)
        rebuild_symbols(write_conn, symbols_root)
        optimize_fts(write_conn)

    mcp = FastMCP("teamcache")

    @mcp.tool()
    def repo_overview() -> str | dict[str, str]:
        try:
            if "data" in _OVERVIEW_CACHE and time.time() - _OVERVIEW_CACHE["ts"] < 60:
                return _OVERVIEW_CACHE["data"]
            result = _repo_overview(repo_root, read_conn)
            _OVERVIEW_CACHE["data"] = result
            _OVERVIEW_CACHE["ts"] = time.time()
            return result
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
                    obj = get_summary(read_conn, key)
                except OSError:
                    pass

            # P1-E: detect moved files — cached path differs from current path
            if obj and obj.get("file_path") != rel:
                return {
                    "summary": obj["summary"],
                    "summary_type": obj.get("summary_type", "ai"),
                    "cached": True,
                    "file_path": file_path,
                    "language": language,
                    "path_mismatch": True,
                    "note": (
                        f"Summary found but was indexed under a different path ({obj['file_path']}). "
                        "File may have been moved. Read the file and call cache_summary() to re-index."
                    ),
                    "summary_confidence": "low",
                    "quality_score": obj.get("quality_score") or compute_quality_score(obj["summary"], obj.get("file_size_bytes") or 0),
                }

            if obj and obj["summary_type"] == "ai":
                # P1-B: enforce "read source before edit" note and add summary_confidence
                note = (
                    "IMPORTANT: You MUST read the exact source file before making any edits. "
                    "This summary is for orientation only. Do not edit based solely on this cache."
                ) if getattr(config, "require_source_before_edit", True) else "Read source before editing."
                confidence = _summary_confidence(obj.get("created_at"))
                return {
                    "summary": obj["summary"],
                    "summary_type": "ai",
                    "cached": True,
                    "file_path": file_path,
                    "language": language,
                    "note": note,
                    "summary_confidence": confidence,
                    "quality_score": obj.get("quality_score") or compute_quality_score(obj["summary"], obj.get("file_size_bytes") or 0),
                }
            if obj and obj["summary_type"] == "static":
                return {
                    "summary": obj["summary"],
                    "summary_type": "static",
                    "cached": True,
                    "file_path": file_path,
                    "language": language,
                    "note": "Static index only. Read file for full understanding, then call cache_summary().",
                    "quality_score": obj.get("quality_score") or compute_quality_score(obj["summary"], obj.get("file_size_bytes") or 0),
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

            summary_bytes = len(summary.encode())
            if summary_bytes > MAX_SUMMARY_BYTES:
                return {"stored": False, "error": f"summary too long ({summary_bytes} bytes, max {MAX_SUMMARY_BYTES})"}
            if len(summary.strip().split()) < MIN_SUMMARY_WORDS:
                return {"stored": False, "error": "summary too short (minimum 5 words)"}

            # P0-C: block sensitive paths (built-in patterns + config denylist)
            if is_sensitive_path(str(rel)):
                return {"stored": False, "error": "sensitive path blocked — not cached"}
            denylist = getattr(config, "sensitive_path_denylist", []) or []
            if denylist:
                rel_lower = rel.lower()
                for pattern in denylist:
                    if pattern and pattern.lower() in rel_lower:
                        return {"stored": False, "error": "sensitive path blocked — not cached"}

            if not path.exists():
                return {"stored": False, "error": "file not found"}
            if not summary or not summary.strip():
                return {"stored": False, "error": "summary must not be empty"}
            if contains_secret(summary):
                return {"stored": False, "error": "summary appears to contain a secret or credential — not cached"}

            content = path.read_bytes()
            fhash = file_hash(content)
            key = cache_key(fhash, config.schema_version)
            if key in _WRITTEN_THIS_SESSION:
                return {"stored": False, "reason": "already written this session"}
            existing = get_summary(read_conn, key)
            if existing and existing["summary_type"] == "ai":
                return {
                    "stored": False,
                    "reason": "ai summary already cached",
                    "cache_key": key,
                }

            created_by = git_user_email(repo_root)
            obj = {
                "cache_key": key,
                "file_path": rel,
                "file_hash": fhash,
                "summary": summary.strip(),
                "summary_type": "ai",
                "schema_version": config.schema_version,
                "created_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "created_by": created_by,
                "file_size_bytes": len(content),
                "language": language,
            }
            store.write(key, obj)
            insert_summary(write_conn, obj)
            upsert_embedding(emb_conn, key, rel, summary.strip(), "ai")
            write_audit_event(write_conn, "write", key, rel, created_by, "ai")
            _WRITTEN_THIS_SESSION.add(key)
            return {"stored": True, "cache_key": key, "summary_type": "ai"}
        except Exception as exc:  # noqa: BLE001
            return {"stored": False, "error": str(exc)}

    @mcp.tool()
    def get_audit_log(file_path: str = "", limit: int = 20) -> list[dict[str, Any]]:
        try:
            bounded_limit = max(1, min(int(limit), 100))
            if file_path:
                rel, _path = _sanitize_path(repo_root, repo_resolved, file_path)
                if rel is None:
                    return [{"error": "path outside repository"}]
                rows = read_conn.execute(
                    """
                    SELECT id, event_type, cache_key, file_path, created_by, summary_type, occurred_at
                    FROM audit_log
                    WHERE file_path = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (rel, bounded_limit),
                ).fetchall()
            else:
                rows = read_conn.execute(
                    """
                    SELECT id, event_type, cache_key, file_path, created_by, summary_type, occurred_at
                    FROM audit_log
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 - MCP tools must report errors as data.
            return [{"error": str(exc)}]

    @mcp.tool()
    async def find_relevant_files(task: str) -> list[dict[str, Any]]:
        try:
            if not task or not task.strip():
                return []
            semantic = _semantic_results(read_conn, emb_conn, task, limit=5)
            keyword = keyword_search(read_conn, task, limit=5)
            results = _merge_relevance_results(semantic, keyword, limit=5)
            if config.scope_paths:
                results = [r for r in results if any(r["file_path"].startswith(p) for p in config.scope_paths)]
            mtimes = await asyncio.gather(
                *[_git_mtime(repo_root, r["file_path"]) for r in results]
            )
            now = datetime.now(tz=timezone.utc)
            for result, mtime in zip(results, mtimes):
                result["file_last_modified"] = mtime
                # Fetch created_at from DB — not included in search result dicts
                summary_obj = get_summary_by_path(read_conn, result["file_path"])
                created_at_str = summary_obj.get("created_at") if summary_obj else None
                summary_age_days: int | None = None
                if created_at_str:
                    try:
                        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        summary_age_days = (now - created).days
                    except (ValueError, TypeError):
                        pass
                result["summary_age_days"] = summary_age_days
                stale = False
                if mtime and created_at_str:
                    try:
                        git_ts = datetime.fromisoformat(mtime.replace(" ", "T", 1))
                        if git_ts.tzinfo is None:
                            git_ts = git_ts.replace(tzinfo=timezone.utc)
                        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        stale = git_ts > created
                    except (ValueError, TypeError):
                        pass
                result["stale"] = stale
            # stale=False first (fresh summaries), then by quality_score DESC
            results.sort(key=lambda r: (r.get("stale") or False, -(r.get("quality_score") or 0.0)))
            used_semantic = len(semantic) > 0
            search_mode = "semantic+keyword" if used_semantic else "keyword_only"
            return [{"search_mode": search_mode, **r} for r in results]
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
            obj = get_symbol_by_path(read_conn, rel)
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
            for result in find_symbol_by_name(read_conn, symbol_name, limit=10):
                obj = get_symbol(read_conn, result["cache_key"])
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
    async def get_changed_context(since_branch: str = "main") -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only", f"{since_branch}...HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_root,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            return {"error": "git diff failed", "since_branch": since_branch}
        if proc.returncode != 0:
            return {"error": "git diff failed", "since_branch": since_branch}

        rels = [line for line in stdout.decode().splitlines() if line.strip()]
        mtimes = await asyncio.gather(*[_git_mtime(repo_root, rel) for rel in rels])

        changed_files = []
        files_needing_review = []
        ai_count = 0
        for rel, mtime in zip(rels, mtimes):
            obj = get_summary_by_path(read_conn, rel)
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
                    "last_modified": mtime,
                }
            )
        total = len(changed_files)
        return {
            "since_branch": since_branch,
            "changed_files": changed_files,
            "files_needing_review": files_needing_review,
            "ai_coverage": f"{ai_count} of {total} changed files have AI summaries",
        }

    @mcp.tool()
    def get_dependents(file_path: str) -> dict[str, Any]:
        try:
            repomap_path = repo_root / ".teamcache" / "objects" / "repomap.json"
            if not repomap_path.exists():
                return {"error": "repomap not built yet, run: teamcache index"}
            repomap = json.loads(repomap_path.read_text(encoding="utf-8"))
            dependents = repomap.get("reverse_imports", {}).get(file_path, [])
            results = []
            for dep_path in dependents:
                obj = get_summary_by_path(read_conn, dep_path)
                results.append({
                    "file_path": dep_path,
                    "summary": obj["summary"] if obj else None,
                    "summary_type": obj["summary_type"] if obj else None,
                })
            return {"file_path": file_path, "dependents": results, "count": len(results)}
        except Exception as exc:  # noqa: BLE001 - MCP tools must report errors as data.
            return {"error": str(exc)}

    try:
        mcp.run(transport="stdio")
    finally:
        read_conn.close()
        write_conn.close()
        emb_conn.close()


async def _git_mtime(repo_root: Path, file_path: str) -> str | None:
    """Return the ISO timestamp of the most recent git commit touching file_path, or None.

    Results are cached in _GIT_MTIME_CACHE for the server's lifetime.
    """
    if file_path in _GIT_MTIME_CACHE:
        return _GIT_MTIME_CACHE[file_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--format=%ci", "-1", "--", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=repo_root,
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode().strip() or None
    except OSError:
        result = None
    _GIT_MTIME_CACHE[file_path] = result
    return result


def _summary_confidence(created_at: str | None) -> str:
    """Compute confidence tier based on how old the cached summary is."""
    if not created_at:
        return "unknown"
    try:
        # Handle both Z-suffix and +00:00 offset ISO strings
        ts = created_at.replace("Z", "+00:00")
        created = datetime.fromisoformat(ts)
        now = datetime.now(tz=timezone.utc)
        age_days = (now - created).days
        if age_days < 7:
            return "high"
        if age_days < 30:
            return "medium"
        return "low"
    except (ValueError, TypeError):
        return "unknown"


def _repo_overview(repo_root: Path, conn: Any) -> str:
    repomap = _read_repomap(repo_root)
    if repomap is None:
        return _repo_overview_from_scan(repo_root, conn)

    tree = _tree_from_repomap(repo_root, repomap)
    raw_languages = repomap.get("languages", {})
    language_counts = raw_languages if isinstance(raw_languages, dict) else {}
    total_on_disk = _repomap_total_files(repomap, language_counts)
    languages = "\n".join(
        f"- {suffix}: {count}"
        for suffix, count in sorted(language_counts.items(), key=lambda item: item[1], reverse=True)
    )
    coverage = count_by_type(conn)
    top_modules = _top_module_lines(repomap.get("top_symbols", []))
    entry_points = repomap.get("entry_points", [])
    entry_lines = [
        f"- {entry}"
        for entry in entry_points
        if isinstance(entry, str) and entry
    ] if isinstance(entry_points, list) else []

    sections = [
        "## Directory Structure\n"
        f"{tree}",
        "## Languages\n"
        f"{languages or '- none found'}",
        "## Summary Coverage\n"
        f"{coverage['ai']} ai, {coverage['static']} static, {coverage['total']} indexed of {total_on_disk} total files",
    ]
    if top_modules:
        sections.append("## Top Modules\n" + "\n".join(top_modules))
    if entry_lines:
        sections.append("## Entry Points\n" + "\n".join(entry_lines))
    return "\n\n".join(sections)


def _repo_overview_from_scan(repo_root: Path, conn: Any) -> str:
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


def _read_repomap(repo_root: Path) -> dict[str, Any] | None:
    repomap_path = repo_root / ".teamcache" / "objects" / "repomap.json"
    if not repomap_path.exists():
        return None
    try:
        repomap = json.loads(repomap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return repomap if isinstance(repomap, dict) else None


def _repomap_total_files(repomap: dict[str, Any], language_counts: dict[str, Any]) -> int:
    total = repomap.get("total_files")
    if isinstance(total, int):
        return total
    return sum(count for count in language_counts.values() if isinstance(count, int))


def _top_module_lines(top_symbols: Any) -> list[str]:
    if not isinstance(top_symbols, list):
        return []
    lines: list[str] = []
    seen_paths: set[str] = set()
    for item in top_symbols:
        if not isinstance(item, dict):
            continue
        file_path = item.get("file_path")
        if not isinstance(file_path, str) or not file_path or file_path in seen_paths:
            continue
        language = item.get("language")
        language_text = language if isinstance(language, str) and language else "unknown"
        lines.append(f"- {file_path} ({language_text})")
        seen_paths.add(file_path)
        if len(lines) == 10:
            break
    return lines


def _tree_from_repomap(repo_root: Path, repomap: dict[str, Any]) -> str:
    paths = _repomap_paths(repomap)
    if not paths:
        return repo_root.name + "/"

    root: dict[str, Any] = {}
    for path in sorted(paths):
        current = root
        parts = [part for part in Path(path).parts if part not in {"", "."}]
        for part in parts:
            current = current.setdefault(part, {})

    lines = [repo_root.name + "/"]
    _append_tree_lines(lines, root, depth=1)
    return "\n".join(lines)


def _repomap_paths(repomap: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    top_symbols = repomap.get("top_symbols", [])
    if isinstance(top_symbols, list):
        for item in top_symbols:
            if isinstance(item, dict) and isinstance(item.get("file_path"), str):
                paths.add(item["file_path"])

    entry_points = repomap.get("entry_points", [])
    if isinstance(entry_points, list):
        paths.update(entry for entry in entry_points if isinstance(entry, str))

    reverse_imports = repomap.get("reverse_imports", {})
    if isinstance(reverse_imports, dict):
        for file_path, importers in reverse_imports.items():
            if isinstance(file_path, str):
                paths.add(file_path)
            if isinstance(importers, list):
                paths.update(importer for importer in importers if isinstance(importer, str))
    return paths


def _append_tree_lines(lines: list[str], node: dict[str, Any], depth: int) -> None:
    indent = "  " * depth
    for name, child in sorted(node.items()):
        suffix = "/" if child else ""
        lines.append(f"{indent}{name}{suffix}")
        if child:
            _append_tree_lines(lines, child, depth + 1)


def _repo_scan(repo_root: Path) -> tuple[str, dict[str, int]]:
    # Directory tree: use os.walk (unchanged)
    lines = [repo_root.name + "/"]
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
            if depth <= 2:
                lines.append(f"{indent}{filename}")

    # P2-B: language counts from git ls-files (tracked files only)
    language_counts: dict[str, int] = {}
    try:
        ls_result = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if ls_result.returncode == 0:
            for raw in ls_result.stdout.split(b"\x00"):
                if not raw:
                    continue
                rel = raw.decode("utf-8", errors="replace")
                suffix = Path(rel).suffix.lower()
                if suffix in SKIP_SUFFIXES or suffix in BINARY_EXTENSIONS:
                    continue
                lang = LANGUAGE_BY_SUFFIX.get(suffix, suffix or "<none>")
                language_counts[lang] = language_counts.get(lang, 0) + 1
        else:
            # Fallback to os.walk-based counting if git is unavailable
            language_counts = _language_counts_from_walk(repo_root)
    except (OSError, subprocess.TimeoutExpired):
        language_counts = _language_counts_from_walk(repo_root)

    return "\n".join(lines), language_counts


def _language_counts_from_walk(repo_root: Path) -> dict[str, int]:
    """Fallback: count languages via os.walk when git ls-files is unavailable."""
    language_counts: dict[str, int] = {}
    for dirpath, dirs, files in os.walk(repo_root):
        dirs[:] = [dirname for dirname in dirs if dirname not in SKIP_DIRS]
        for filename in files:
            suffix = Path(filename).suffix.lower()
            if suffix in SKIP_SUFFIXES or suffix in BINARY_EXTENSIONS:
                continue
            lang = LANGUAGE_BY_SUFFIX.get(suffix, suffix or "<none>")
            language_counts[lang] = language_counts.get(lang, 0) + 1
    return language_counts


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
