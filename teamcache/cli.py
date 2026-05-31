from __future__ import annotations

import os
import shutil
import subprocess
import sys
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.progress import track

from .config import TeamCacheConfig, find_repo_root, load_config, write_config
from .constants import (
    EMBEDDINGS_DB,
    ENTRY_POINTS,
    INDEX_DB,
    LANGUAGE_BY_SUFFIX,
    LOCAL_DIR,
    SUMMARIES_DIR,
    SYMBOLS_DIR,
)
from .db import (
    connect,
    coverage_stats,
    get_summary,
    get_symbol,
    insert_summary,
    insert_symbol,
    invalidate_all,
    invalidate_by_path,
    invalidate_stale,
    optimize_fts,
    rebuild_from_objects,
    rebuild_symbols,
    static_only_files,
    summary_count,
    symbol_count,
    top_contributors,
)
from .embeddings import connect_embeddings, upsert_embedding
from .files import (
    cache_key,
    file_hash,
    git_user_email,
    is_binary_content,
    iter_repo_files,
    should_skip_metadata,
    write_summary_object,
)
from .symbols import extract_symbols, summary_from_symbols, write_symbol_object

console = Console()


@click.group()
def cli() -> None:
    """TeamCache shared repository context cache."""


@cli.command()
@click.option("--enable-hooks", is_flag=True, default=False, help="Install .githooks/post-merge hook.")
@click.option("--force-hooks", is_flag=True, default=False, help="Overwrite existing hooks even if conflicts exist.")
def init(enable_hooks: bool, force_hooks: bool) -> None:
    repo_root = Path.cwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        is_git_repo = result.returncode == 0
    except OSError:
        is_git_repo = False
    if not is_git_repo:
        console.print("[yellow]warning:[/yellow] not inside a Git repository")

    (repo_root / SUMMARIES_DIR).mkdir(parents=True, exist_ok=True)
    (repo_root / SYMBOLS_DIR).mkdir(parents=True, exist_ok=True)
    (repo_root / LOCAL_DIR).mkdir(parents=True, exist_ok=True)
    write_config(repo_root, TeamCacheConfig())
    _ensure_gitignore_entry(repo_root / ".gitignore", ".teamcache/local/")

    # Warn about stale post-commit hook left by v0.1.x
    if (repo_root / ".githooks" / "post-commit").exists():
        console.print(
            "[yellow]warning:[/yellow] .githooks/post-commit exists from a previous install. "
            "Run: teamcache migrate-hooks"
        )

    if enable_hooks:
        conflicts = _detect_hook_conflicts(repo_root)
        if conflicts and not force_hooks:
            console.print(
                f"[red]error:[/red] existing hooks detected in .git/hooks/: {', '.join(conflicts)}. "
                "Use --force-hooks to overwrite."
            )
            raise SystemExit(1)
        if _install_post_merge_hook(repo_root, set_hooks_path=True):
            console.print("Git hook installed: .githooks/post-merge")
    else:
        console.print("Hooks not installed. Run: teamcache init --enable-hooks")

    console.print("Run: teamcache index")
    console.print("Then: teamcache install")


@cli.command("migrate-hooks")
def migrate_hooks() -> None:
    """Remove dangerous amend lines from .githooks/post-commit left by v0.1.x."""
    root = find_repo_root(Path.cwd())
    hook = root / ".githooks" / "post-commit"
    if not hook.exists():
        console.print("No .githooks/post-commit found. Nothing to do.")
        return
    content = hook.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    cleaned = [l for l in lines if "amend" not in l]
    if len(cleaned) == len(lines):
        console.print("No amend lines found. Hook is already clean.")
        return
    _atomic_write_text(hook, "".join(cleaned))
    console.print(f"Removed {len(lines) - len(cleaned)} amend line(s) from .githooks/post-commit.")


@cli.command()
def index() -> None:
    root = find_repo_root(Path.cwd())
    config = load_config(root)
    conn = connect(root / INDEX_DB)
    emb_conn = connect_embeddings(root / EMBEDDINGS_DB)
    indexed = 0
    skipped = 0
    errors = 0

    try:
        rebuild_from_objects(conn, root / SUMMARIES_DIR)
        rebuild_symbols(conn, root / SYMBOLS_DIR)
        files = list(iter_repo_files(root))
        created_by = git_user_email(root)
        for path in track(files, description="Indexing"):
            try:
                result = _index_file(root, config.schema_version, conn, emb_conn, path, created_by)
                if result is None:
                    skipped += 1
                    continue
                if result["skipped"]:
                    skipped += 1
                    continue
                indexed += 1
            except Exception as exc:  # noqa: BLE001 - indexing should continue per file.
                errors += 1
                console.print(f"[yellow]warning:[/yellow] failed to index {path}: {exc}")
        _write_repomap(root)
        optimize_fts(conn)
        console.print(f"Indexed: {indexed}  Skipped: {skipped}")
        if errors:
            console.print(f"Errors: {errors}")
    finally:
        conn.close()
        emb_conn.close()


@cli.command()
@click.option("--since", "since_branch", default="main", show_default=True)
def changed(since_branch: str) -> None:
    root = find_repo_root(Path.cwd())
    config = load_config(root)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{since_branch}...HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        console.print(f"[yellow]warning:[/yellow] git diff failed: {escape(str(exc))}")
        return
    if result.returncode != 0:
        detail = _first_warning_line(result.stderr.strip() or result.stdout.strip())
        console.print(f"[yellow]warning:[/yellow] git diff failed: {escape(detail)}")
        return

    conn = connect(root / INDEX_DB)
    emb_conn = connect_embeddings(root / EMBEDDINGS_DB)
    refreshed = 0
    changed_count = 0
    created_by = git_user_email(root)
    repo_resolved = root.resolve()
    try:
        for rel in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            path = (root / rel).resolve()
            if not path.is_relative_to(repo_resolved) or not path.is_file():
                continue
            changed_count += 1
            rel_path = path.relative_to(repo_resolved).as_posix()
            invalidate_by_path(conn, rel_path)
            index_result = _index_file(root, config.schema_version, conn, emb_conn, path, created_by)
            if index_result and not index_result["skipped"]:
                refreshed += 1
        optimize_fts(conn)
    finally:
        conn.close()
        emb_conn.close()
    console.print(f"{changed_count} files changed, {refreshed} summaries refreshed")


@cli.command()
@click.option("--quiet", is_flag=True, help="Suppress output.")
def sync(quiet: bool) -> None:
    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        invalidate_all(conn)
        rebuild_from_objects(conn, root / SUMMARIES_DIR)
        rebuild_symbols(conn, root / SYMBOLS_DIR)
        optimize_fts(conn)
        summaries = summary_count(conn)
        symbols = symbol_count(conn)
    finally:
        conn.close()
    if not quiet:
        console.print(f"Synced: {summaries} summaries, {symbols} symbol files")


@cli.command()
@click.argument("files", nargs=-1)
@click.option("--stale", is_flag=True, help="Invalidate entries older than 30 days.")
@click.option("--all", "all_entries", is_flag=True, help="Clear the full local index.")
@click.option("--quiet", is_flag=True, help="Suppress output.")
@click.option("--stdin", is_flag=True, default=False, help="Read file paths from stdin, one per line.")
def invalidate(files: tuple[str, ...], stale: bool, all_entries: bool, quiet: bool, stdin: bool) -> None:
    if stale and all_entries:
        raise click.ClickException("--stale and --all are mutually exclusive")

    # Collect paths from stdin if requested
    stdin_files: list[str] = []
    if stdin:
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("--"):
                continue
            stdin_files.append(stripped)

    all_files = list(files) + stdin_files

    if not stale and not all_entries and not all_files:
        if not quiet:
            raise click.ClickException("provide file path(s), --stale, or --all")
        return

    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        if all_entries:
            invalidate_all(conn)
            if not quiet:
                console.print("Invalidated: all entries. Run teamcache index to rebuild.")
        elif stale:
            count = invalidate_stale(conn)
            if not quiet:
                console.print(f"Invalidated: {count} stale entries")
        else:
            for file_path in all_files:
                rel = file_path.replace("\\", "/").lstrip("/")
                invalidate_by_path(conn, rel)
            if not quiet:
                console.print(f"Invalidated: {len(all_files)} file(s)")
    finally:
        conn.close()


@cli.command()
def stats() -> None:
    from rich.panel import Panel

    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        cov = coverage_stats(conn)
        contributors = top_contributors(conn)
        uncached = static_only_files(conn)
    finally:
        conn.close()

    total_disk = sum(1 for _ in iter_repo_files(root))
    ai = cov["ai"]
    static = cov["static"]
    indexed = cov["indexed"]
    not_indexed = max(0, total_disk - indexed)

    def pct(n: int, d: int) -> str:
        return f"{round(n / d * 100)}%" if d else "0%"

    lines = [
        f"Files total:      {total_disk:>6,}",
        f"AI summaries:     {ai:>6,}  ({pct(ai, total_disk)})",
        f"Static summaries: {static:>6,}  ({pct(static, total_disk)})",
        f"Not indexed:      {not_indexed:>6,}  ({pct(not_indexed, total_disk)})",
        f"Cache hit rate:   {pct(ai, indexed) if indexed else '0%':>6}",
    ]
    if contributors:
        lines += ["", "Top contributors (AI summaries):"]
        for c in contributors:
            lines.append(f"  {c['email']:<35} → {c['count']:,} objects")
    if uncached:
        lines += ["", "Most uncached files:"]
        for path in uncached:
            lines.append(f"  {path}  (static only)")

    console.print(Panel("\n".join(lines), title="TeamCache Stats", border_style="blue"))


@cli.command()
def report() -> None:
    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        cov = coverage_stats(conn)
        contributors = top_contributors(conn, limit=10)
        uncached = static_only_files(conn, limit=20)
    finally:
        conn.close()

    total_disk = sum(1 for _ in iter_repo_files(root))
    ai = cov["ai"]
    static = cov["static"]
    indexed = cov["indexed"]
    not_indexed = max(0, total_disk - indexed)

    def pct(n: int, d: int) -> str:
        return f"{round(n / d * 100)}%" if d else "0%"

    now = datetime.utcnow()
    month_str = now.strftime("%Y-%m")
    lines = [
        f"# TeamCache Report — {month_str}",
        "",
        f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Summary Coverage",
        "",
        "| Metric | Count | % |",
        "|---|---|---|",
        f"| AI summaries | {ai:,} | {pct(ai, total_disk)} |",
        f"| Static summaries | {static:,} | {pct(static, total_disk)} |",
        f"| Not indexed | {not_indexed:,} | {pct(not_indexed, total_disk)} |",
        f"| **Total files** | **{total_disk:,}** | 100% |",
        "",
        f"Cache hit rate (AI / indexed): **{pct(ai, indexed) if indexed else '0%'}**",
        "",
    ]
    if contributors:
        lines += [
            "## Top Contributors",
            "",
            "| Developer | AI Summaries |",
            "|---|---|",
        ]
        for c in contributors:
            lines.append(f"| {c['email']} | {c['count']:,} |")
        lines.append("")
    if uncached:
        lines += [
            "## Hottest Files Still on Static Summary",
            "",
            "_Prime targets: read these to generate AI summaries for the whole team._",
            "",
        ]
        for path in uncached:
            lines.append(f"- `{path}`")
        lines.append("")

    reports_dir = root / ".teamcache" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{month_str}.md"
    _atomic_write_text(report_path, "\n".join(lines) + "\n")
    console.print(f"Report written to {report_path.relative_to(root)}")


@cli.command()
def commit() -> None:
    try:
        root = find_repo_root(Path.cwd())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    # P2-C: Abort if the user already has staged changes
    pre_staged = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=root,
        check=False,
    )
    if pre_staged.returncode != 0:
        console.print(
            "[red]error:[/red] You have staged changes. "
            "Commit your changes first, then run teamcache commit."
        )
        raise SystemExit(1)

    objects_dir = str(root / ".teamcache" / "objects")
    try:
        subprocess.run(["git", "add", objects_dir], cwd=root, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"git add failed with exit code {exc.returncode}") from exc

    staged = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=root,
        check=False,
    )
    if staged.returncode == 0:
        console.print("Nothing to commit in .teamcache/objects/")
        return

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    try:
        subprocess.run(
            ["git", "commit", "-m", f"chore: update teamcache [{timestamp}]"],
            cwd=root,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"git commit failed with exit code {exc.returncode}") from exc
    console.print("Committed .teamcache/objects/")


@cli.command()
def serve() -> None:
    from .mcp_server import run_server

    run_server()


_SUPPORTED_AGENTS = ("claude", "codex", "cursor", "opencode", "aider", "windsurf")


@cli.command()
@click.option(
    "--agent",
    default="claude",
    show_default=True,
    type=click.Choice(_SUPPORTED_AGENTS),
    help="AI tool to register with.",
)
def install(agent: str) -> None:
    try:
        repo_root = find_repo_root(Path.cwd())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    teamcache_bin = _resolve_teamcache_bin()
    _agent_install(agent, repo_root, teamcache_bin)


@cli.command()
@click.option(
    "--agent",
    default="claude",
    show_default=True,
    type=click.Choice(_SUPPORTED_AGENTS),
    help="AI tool to unregister from.",
)
def uninstall(agent: str) -> None:
    try:
        repo_root = find_repo_root(Path.cwd())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _agent_uninstall(agent, repo_root)


def _resolve_teamcache_bin() -> Path:
    venv_bin = Path(sys.executable).parent / "teamcache"
    if os.name == "nt":
        windows_bin = Path(str(venv_bin) + ".exe")
        if windows_bin.exists():
            return windows_bin
        if venv_bin.exists():
            return venv_bin
    else:
        if venv_bin.exists():
            return venv_bin
    found = shutil.which("teamcache")
    if not found:
        raise click.ClickException(
            "teamcache executable not found in venv or PATH; "
            "ensure teamcache is installed in the active Python environment"
        )
    return Path(found)


def _agent_install(agent: str, repo_root: Path, teamcache_bin: Path) -> None:
    if agent == "claude":
        if not shutil.which("claude"):
            raise click.ClickException("'claude' not found in PATH")
        try:
            subprocess.run(
                ["claude", "mcp", "add", "teamcache", "--scope", "project",
                 "--", str(teamcache_bin), "serve"],
                cwd=repo_root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise click.ClickException(f"claude mcp add failed: exit {exc.returncode}") from exc
        except OSError as exc:
            raise click.ClickException(f"failed to run claude: {exc}") from exc
        _append_claude_instructions(repo_root / "CLAUDE.md")
        console.print("Claude Code: MCP registered, CLAUDE.md updated.")

    elif agent == "codex":
        mcp_config = repo_root / ".codex" / "config.json"
        _write_mcp_json(mcp_config, str(teamcache_bin))
        _append_instructions_block(
            repo_root / "AGENTS.md",
            _AGENTS_INSTRUCTIONS_BLOCK,
            _AGENTS_SENTINEL,
        )
        console.print("Codex: .codex/config.json written, AGENTS.md updated.")

    elif agent == "cursor":
        mcp_config = repo_root / ".cursor" / "mcp.json"
        _write_mcp_json(mcp_config, str(teamcache_bin))
        _append_instructions_block(
            repo_root / ".cursorrules",
            _CURSORRULES_BLOCK,
            _AGENTS_SENTINEL,
        )
        console.print("Cursor: .cursor/mcp.json written, .cursorrules updated.")

    elif agent == "opencode":
        mcp_config = repo_root / ".opencode" / "config.json"
        _write_mcp_json(mcp_config, str(teamcache_bin))
        _append_instructions_block(
            repo_root / "AGENTS.md",
            _AGENTS_INSTRUCTIONS_BLOCK,
            _AGENTS_SENTINEL,
        )
        console.print("OpenCode: .opencode/config.json written, AGENTS.md updated.")

    elif agent == "aider":
        # Aider has no MCP; inject instructions via --read config
        instructions_path = repo_root / ".teamcache-instructions.md"
        _atomic_write_text(instructions_path, _AIDER_INSTRUCTIONS_FILE)
        _write_aider_config(repo_root / ".aider.conf.yml", ".teamcache-instructions.md")
        console.print("Aider: .teamcache-instructions.md written, .aider.conf.yml updated.")

    elif agent == "windsurf":
        mcp_config = repo_root / ".windsurf" / "mcp.json"
        _write_mcp_json(mcp_config, str(teamcache_bin))
        _append_instructions_block(
            repo_root / ".windsurfrules",
            _CURSORRULES_BLOCK,
            _AGENTS_SENTINEL,
        )
        console.print("Windsurf: .windsurf/mcp.json written, .windsurfrules updated.")


def _agent_uninstall(agent: str, repo_root: Path) -> None:
    if agent == "claude":
        if shutil.which("claude"):
            try:
                subprocess.run(
                    ["claude", "mcp", "remove", "teamcache", "--scope", "project"],
                    cwd=repo_root,
                    check=False,
                )
            except OSError:
                pass
        _remove_instructions_block(repo_root / "CLAUDE.md", _CLAUDE_SENTINEL)
        console.print("Claude Code: MCP removed, CLAUDE.md cleaned.")

    elif agent == "codex":
        _remove_mcp_json(repo_root / ".codex" / "config.json")
        _remove_instructions_block(repo_root / "AGENTS.md", _AGENTS_SENTINEL)
        console.print("Codex: config removed.")

    elif agent == "cursor":
        _remove_mcp_json(repo_root / ".cursor" / "mcp.json")
        _remove_instructions_block(repo_root / ".cursorrules", _AGENTS_SENTINEL)
        console.print("Cursor: config removed.")

    elif agent == "opencode":
        _remove_mcp_json(repo_root / ".opencode" / "config.json")
        _remove_instructions_block(repo_root / "AGENTS.md", _AGENTS_SENTINEL)
        console.print("OpenCode: config removed.")

    elif agent == "aider":
        _remove_aider_config(repo_root / ".aider.conf.yml", ".teamcache-instructions.md")
        instructions = repo_root / ".teamcache-instructions.md"
        if instructions.exists():
            instructions.unlink()
        console.print("Aider: config removed.")

    elif agent == "windsurf":
        _remove_mcp_json(repo_root / ".windsurf" / "mcp.json")
        _remove_instructions_block(repo_root / ".windsurfrules", _AGENTS_SENTINEL)
        console.print("Windsurf: config removed.")


_AGENTS_SENTINEL = "<!-- teamcache-installed -->"

_AGENTS_INSTRUCTIONS_BLOCK = """\
<!-- teamcache-installed -->
## TeamCache
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path)
  - summary_type "ai"     -> use summary, skip reading (unless editing)
  - summary_type "static" -> use for navigation, still read the file,
                              then call cache_summary() with your understanding
  - cached false          -> read file normally, then call cache_summary()
After reading any file (when cached was not "ai"): call
  cache_summary(file_path, your_understanding, language)
Before any task: call find_relevant_files(task_description)
"""

_CURSORRULES_BLOCK = """\
<!-- teamcache-installed -->
# TeamCache
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path).
After reading any file (when not ai-cached): call cache_summary(file_path, summary, language).
Before any task: call find_relevant_files(task_description).
"""

_AIDER_INSTRUCTIONS_FILE = """\
# TeamCache Instructions
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path).
After reading any file (when not ai-cached): call cache_summary(file_path, summary, language).
Before any task: call find_relevant_files(task_description).
"""


def _write_mcp_json(config_path: Path, teamcache_bin: str) -> None:
    import json as _json
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if config_path.exists():
        try:
            existing = _json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
    servers = existing.setdefault("mcpServers", {})
    servers["teamcache"] = {"command": teamcache_bin, "args": ["serve"]}
    _atomic_write_text(config_path, _json.dumps(existing, indent=2) + "\n")


def _remove_mcp_json(config_path: Path) -> None:
    import json as _json
    if not config_path.exists():
        return
    try:
        data = _json.loads(config_path.read_text(encoding="utf-8"))
        data.get("mcpServers", {}).pop("teamcache", None)
        _atomic_write_text(config_path, _json.dumps(data, indent=2) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _append_instructions_block(path: Path, block: str, sentinel: str) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if sentinel in content:
            return
        newline = "\r\n" if "\r\n" in content else "\n"
    else:
        content = ""
        newline = os.linesep
    prefix = content
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix:
        prefix += newline
    _atomic_write_text(path, prefix + block.replace("\n", newline))


def _remove_instructions_block(path: Path, sentinel: str) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    if sentinel not in content:
        return
    # Remove everything from the sentinel line to the next blank-line-separated section
    # Strategy: remove from sentinel to end of file (block is always appended at end)
    idx = content.find(sentinel)
    if idx == -1:
        return
    # Walk back to the start of that line
    start = content.rfind("\n", 0, idx)
    start = 0 if start == -1 else start
    trimmed = content[:start].rstrip()
    _atomic_write_text(path, (trimmed + "\n") if trimmed else "")


def _write_aider_config(config_path: Path, instructions_rel: str) -> None:
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        if instructions_rel in content:
            return
        newline = "\r\n" if "\r\n" in content else "\n"
        prefix = content
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        _atomic_write_text(config_path, prefix + f"read:\n  - {instructions_rel}\n".replace("\n", newline))
    else:
        _atomic_write_text(config_path, f"read:\n  - {instructions_rel}\n")


def _remove_aider_config(config_path: Path, instructions_rel: str) -> None:
    if not config_path.exists():
        return
    import re as _re
    content = config_path.read_text(encoding="utf-8")
    content = _re.sub(rf"\n?\s*-\s*{_re.escape(instructions_rel)}\n?", "\n", content)
    _atomic_write_text(config_path, content)


def _index_file(
    root: Path,
    schema_version: str,
    conn,
    emb_conn,
    path: Path,
    created_by: str,
) -> dict[str, object] | None:
    if should_skip_metadata(path):
        return None
    content = path.read_bytes()
    if is_binary_content(path, content):
        return None

    fhash = file_hash(content)
    key = cache_key(fhash, schema_version)
    language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown")
    rel_path = path.resolve().relative_to(root.resolve()).as_posix()
    existing_summary = get_summary(conn, key)
    existing_symbol = get_symbol(conn, key)
    summary_needs_upsert = (
        existing_summary is None or existing_summary.get("file_path") != rel_path
    )
    symbol_needs_upsert = existing_symbol is None or existing_symbol.get("file_path") != rel_path
    if not summary_needs_upsert and not symbol_needs_upsert:
        return {"skipped": True}

    text = content.decode("utf-8", errors="ignore")
    disk_symbol_obj = _read_symbol_object(root / SYMBOLS_DIR, key)
    symbols = (
        disk_symbol_obj.get("symbols", {})
        if isinstance(disk_symbol_obj, dict)
        else extract_symbols(path, language, text)
    )
    if not isinstance(symbols, dict):
        symbols = extract_symbols(path, language, text)
    summary = summary_from_symbols(symbols, language)
    created_at = datetime.utcnow().isoformat() + "Z"
    disk_summary_obj = _read_summary_object(root / SUMMARIES_DIR, key)
    summary_obj = _summary_object_for_path(
        disk_summary_obj,
        rel_path,
        fhash,
        len(content),
        language,
    ) or {
        "cache_key": key,
        "file_path": rel_path,
        "file_hash": fhash,
        "summary": summary,
        "summary_type": "static",
        "schema_version": schema_version,
        "created_at": created_at,
        "created_by": created_by,
        "file_size_bytes": len(content),
        "language": language,
    }
    symbol_obj = _symbol_object_for_path(disk_symbol_obj, rel_path, fhash, language) or {
        "cache_key": key,
        "file_path": rel_path,
        "file_hash": fhash,
        "schema_version": schema_version,
        "language": language,
        "symbols": symbols,
        "created_at": created_at,
        "created_by": created_by,
    }
    if summary_needs_upsert:
        if disk_summary_obj is None:
            write_summary_object(root / SUMMARIES_DIR, key, summary_obj)
        insert_summary(conn, summary_obj)
        upsert_embedding(
            emb_conn,
            key,
            rel_path,
            str(summary_obj["summary"]),
            str(summary_obj["summary_type"]),
        )
    if symbol_needs_upsert:
        if disk_symbol_obj is None:
            write_symbol_object(root / SYMBOLS_DIR, key, symbol_obj)
        insert_symbol(conn, symbol_obj)
    return {
        "skipped": False,
        "language": language,
        "symbol_obj": symbol_obj,
    }


def _read_summary_object(summaries_root: Path, key: str) -> dict[str, object] | None:
    path = summaries_root / key[:2] / f"{key[:12]}_v1.json"
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed cache object should be replaced by a fresh static one.
        return None
    if not isinstance(obj, dict):
        return None
    required = {"cache_key", "file_path", "file_hash", "summary", "summary_type"}
    return obj if required.issubset(obj) else None


def _read_symbol_object(symbols_root: Path, key: str) -> dict[str, object] | None:
    path = symbols_root / key[:2] / f"{key[:12]}_v1.json"
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed cache object should be replaced by a fresh symbol one.
        return None
    if not isinstance(obj, dict):
        return None
    required = {"cache_key", "file_path", "file_hash", "symbols"}
    return obj if required.issubset(obj) and isinstance(obj.get("symbols"), dict) else None


def _summary_object_for_path(
    obj: dict[str, object] | None,
    rel_path: str,
    fhash: str,
    file_size_bytes: int,
    language: str,
) -> dict[str, object] | None:
    if obj is None:
        return None
    updated = dict(obj)
    updated["file_path"] = rel_path
    updated["file_hash"] = fhash
    updated["file_size_bytes"] = file_size_bytes
    updated["language"] = language
    return updated


def _symbol_object_for_path(
    obj: dict[str, object] | None,
    rel_path: str,
    fhash: str,
    language: str,
) -> dict[str, object] | None:
    if obj is None:
        return None
    updated = dict(obj)
    updated["file_path"] = rel_path
    updated["file_hash"] = fhash
    updated["language"] = language
    return updated


def _write_repomap(root: Path) -> None:
    symbol_objects: list[dict[str, object]] = []
    for path in (root / SYMBOLS_DIR).glob("**/*.json"):
        try:
            symbol_objects.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - repomap generation should not block indexing.
            continue
    language_counts: Counter[str] = Counter(
        str(obj.get("language", "unknown")) for obj in symbol_objects
    )
    imports: Counter[str] = Counter()
    definition_locations: dict[str, tuple[str, str]] = {}
    for obj in symbol_objects:
        symbols = obj.get("symbols", {})
        if not isinstance(symbols, dict):
            continue
        file_path = str(obj.get("file_path", ""))
        language = str(obj.get("language", ""))
        for alias in _module_aliases(file_path):
            definition_locations.setdefault(alias, (file_path, language))
        for import_name in symbols.get("imports", []):
            imports[str(import_name)] += 1
        for group_name in ("classes", "functions", "types"):
            for item in symbols.get(group_name, []):
                if isinstance(item, dict) and item.get("name"):
                    definition_locations.setdefault(str(item["name"]), (file_path, language))
    top_symbols = []
    for name, _count in imports.most_common(50):
        file_path = ""
        language = ""
        for lookup_key in _import_lookup_keys(name):
            file_path, language = definition_locations.get(lookup_key, ("", ""))
            if file_path:
                break
        if not file_path:
            continue
        top_symbols.append({"name": name, "file_path": file_path, "language": language})

    repomap = {
        "schema_version": "v1",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_files": sum(language_counts.values()),
        "languages": dict(sorted(language_counts.items())),
        "entry_points": sorted(entry for entry in ENTRY_POINTS if (root / entry).exists()),
        "top_symbols": top_symbols,
    }
    _atomic_write_text(
        root / ".teamcache" / "objects" / "repomap.json",
        json.dumps(repomap, indent=2) + "\n",
    )


def _module_aliases(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.suffix:
        return []
    module = path.with_suffix("").as_posix().replace("/", ".")
    aliases = [module, path.stem]
    if "." in module:
        aliases.append(module.rsplit(".", 1)[1])
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _import_lookup_keys(import_name: str) -> list[str]:
    cleaned = import_name.strip().strip("\"'`")
    cleaned = cleaned.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    cleaned = cleaned.lstrip(".")
    cleaned = cleaned.lstrip("/")
    without_suffix = str(Path(cleaned).with_suffix("")) if cleaned else cleaned
    dotted = without_suffix.replace("/", ".")
    keys = [cleaned.replace("/", "."), dotted]
    if dotted:
        keys.append(dotted.rsplit(".", 1)[-1])
    if cleaned:
        keys.append(Path(cleaned).stem)
    return list(dict.fromkeys(key for key in keys if key))


_HOOK_SYNC = "teamcache sync --quiet"
_HOOK_INVALIDATE = "git diff -z --name-only ORIG_HEAD HEAD | xargs -0 -r teamcache invalidate --quiet"


def _detect_hook_conflicts(repo_root: Path) -> list[str]:
    """Return names of executable hook files in .git/hooks/ (excluding *.sample)."""
    git_hooks_dir = repo_root / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        return []
    conflicts = []
    for hook_file in git_hooks_dir.iterdir():
        if hook_file.suffix == ".sample":
            continue
        if not hook_file.is_file():
            continue
        # On Windows all files are treated as executable; check execute bit on POSIX
        if os.name != "nt":
            if not os.access(hook_file, os.X_OK):
                continue
        conflicts.append(hook_file.name)
    return sorted(conflicts)


def _install_post_merge_hook(repo_root: Path, set_hooks_path: bool = False) -> bool:
    hooks_dir = repo_root / ".githooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-merge"
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        newline = "\r\n" if "\r\n" in content else "\n"
        updated = content
        if _HOOK_SYNC not in updated:
            prefix = updated
            if prefix and not prefix.endswith(("\n", "\r")):
                prefix += newline
            updated = prefix + _HOOK_SYNC + newline
        if _HOOK_INVALIDATE not in updated:
            prefix = updated
            if prefix and not prefix.endswith(("\n", "\r")):
                prefix += newline
            updated = prefix + _HOOK_INVALIDATE + newline
        if updated != content:
            _atomic_write_text(hook_path, updated)
    else:
        _atomic_write_text(hook_path, f"#!/bin/sh\n{_HOOK_SYNC}\n{_HOOK_INVALIDATE}\n")
    if os.name != "nt":
        try:
            hook_path.chmod(0o755)
        except OSError:
            pass
    if set_hooks_path:
        try:
            result = subprocess.run(
                ["git", "config", "core.hooksPath", ".githooks"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = _first_warning_line(result.stderr.strip() or result.stdout.strip())
                console.print(f"[yellow]warning:[/yellow] git hook config failed: {escape(detail)}")
        except OSError as exc:
            console.print(f"[yellow]warning:[/yellow] git hook config failed: {escape(str(exc))}")
    return True


def _first_warning_line(detail: str) -> str:
    for line in detail.splitlines():
        line = line.strip()
        if line:
            return line
    return "unknown error"


def _ensure_gitignore_entry(path: Path, entry: str) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8", errors="replace")
        newline = "\r\n" if "\r\n" in content else "\n"
        lines = content.splitlines()
    else:
        content = ""
        newline = os.linesep
        lines = []

    if entry in {line.strip() for line in lines}:
        return

    prefix = content
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    _atomic_write_text(path, prefix + entry + newline)


_CLAUDE_SENTINEL = "<!-- teamcache-installed -->"


def _append_claude_instructions(path: Path) -> None:
    block = """<!-- teamcache-installed -->
## TeamCache
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path)
  - summary_type "ai"     -> use summary, skip reading (unless editing)
  - summary_type "static" -> use for navigation, still read the file,
                              then call cache_summary() with your understanding
  - cached false          -> read file normally, then call cache_summary()
After reading any file (when cached was not "ai"): call
  cache_summary(file_path, your_understanding, language)
  Your summary should be plain prose: what the file does, key
  dependencies, important side effects, entry points.
  This is stored in git and shared with your whole team.
Before any task: call find_relevant_files(task_description)
"""
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if _CLAUDE_SENTINEL in content:
            return
        newline = "\r\n" if "\r\n" in content else "\n"
    else:
        content = ""
        newline = os.linesep

    prefix = content
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix:
        prefix += newline
    _atomic_write_text(path, prefix + block.replace("\n", newline))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
