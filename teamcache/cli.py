from __future__ import annotations

import os
import shutil
import subprocess
import sys
import json
import difflib
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
    SCHEMA_VERSION,
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
    metrics_summary,
    optimize_fts,
    rebuild_from_objects,
    rebuild_symbols,
    _run_migrations,
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
)
from .store import make_store
from .symbols import extract_symbols, summary_from_symbols

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
    store = make_store(config, root)
    conn = connect(root / INDEX_DB)
    emb_conn = connect_embeddings(root / EMBEDDINGS_DB)
    indexed = 0
    skipped = 0
    errors = 0

    try:
        rebuild_from_objects(conn, root / SUMMARIES_DIR)
        rebuild_symbols(conn, root / SYMBOLS_DIR)
        files = list(iter_repo_files(root))
        if config.scope_paths:
            files = [f for f in files if any(str(f.relative_to(root)).startswith(p) for p in config.scope_paths)]
            console.print(f"Scoped to: {config.scope_paths}")
        created_by = git_user_email(root)
        for path in track(files, description="Indexing"):
            try:
                result = _index_file(root, config.schema_version, conn, emb_conn, path, created_by, store)
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
    store = make_store(config, root)
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
            index_result = _index_file(root, config.schema_version, conn, emb_conn, path, created_by, store)
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
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json"]), show_default=True)
@click.option("--since", default=None, metavar="DATE", help="Filter metrics since DATE (YYYY-MM-DD).")
def metrics(fmt: str, since: str | None) -> None:
    """Show cache performance metrics."""
    from rich.panel import Panel

    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        data = metrics_summary(conn, since=since)
    finally:
        conn.close()

    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
        return

    def pct(v: float) -> str:
        return f"{v:.1f}%"

    lines = [
        f"AI coverage:           {pct(data['ai_coverage_pct']):>8}",
        f"Static coverage:       {pct(data['static_coverage_pct']):>8}",
        f"Total indexed files:   {data['total_files']:>8,}",
        f"Stale entries (14d):   {data['stale_count']:>8,}",
        f"Est. tokens saved:     {data['estimated_tokens_saved']:>8,}",
    ]
    if data["top_contributors"]:
        lines += ["", "Top contributors (AI summaries):"]
        for c in data["top_contributors"]:
            lines.append(f"  {c['email']:<35} → {c['count']:,} objects")
    if data["daily_hits"]:
        lines += ["", "Daily hits/misses (last 30 days):"]
        for entry in data["daily_hits"]:
            lines.append(
                f"  {entry['date']}  hits: {entry['hit_count']:>5,}  misses: {entry['miss_count']:>5,}"
            )

    console.print(Panel("\n".join(lines), title="TeamCache Metrics", border_style="cyan"))


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


@cli.command()
def migrate() -> None:
    """Apply pending database migrations to the local index."""
    root = find_repo_root(Path.cwd())
    conn = connect(root / INDEX_DB)
    try:
        ran = _run_migrations(conn)
    finally:
        conn.close()
    if ran:
        console.print(f"Applied {len(ran)} migration(s): {ran}")
    else:
        console.print("No pending migrations.")


@cli.command("merge-driver")
@click.argument("base", type=click.Path(exists=True))
@click.argument("ours", type=click.Path(exists=True))
@click.argument("theirs", type=click.Path(exists=True))
def merge_driver(base: str, ours: str, theirs: str) -> None:
    """Git merge driver for .teamcache/objects JSON files.

    Called by git as: teamcache merge-driver %O %A %B
    Writes the winning object to OURS (the file git uses as the result).
    Exits 0 on success, 1 on unresolvable error.
    """
    try:
        base_obj = json.loads(Path(base).read_text(encoding="utf-8"))
        ours_obj = json.loads(Path(ours).read_text(encoding="utf-8"))
        theirs_obj = json.loads(Path(theirs).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]error:[/red] failed to read merge inputs: {escape(str(exc))}")
        raise SystemExit(1)

    winner = _pick_summary_winner(ours_obj, theirs_obj)
    try:
        _atomic_write_text(Path(ours), json.dumps(winner, indent=2) + "\n")
    except OSError as exc:
        console.print(f"[red]error:[/red] failed to write merge result: {escape(str(exc))}")
        raise SystemExit(1)


@cli.command()
def doctor() -> None:
    """Check local TeamCache setup health."""
    import shlex
    import sqlite3

    root = find_repo_root(Path.cwd())
    results: list[tuple[str, str, str, str | None]] = []

    def add(status: str, name: str, detail: str, fix: str | None = None) -> None:
        results.append((status, name, detail, fix))

    # CHECK 1 — git identity
    try:
        email_result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        email = email_result.stdout.strip()
    except OSError:
        email = ""
    if email:
        add("PASS", "git identity", email)
    else:
        add(
            "FAIL",
            "git identity",
            "git user.email is not configured",
            "Run: git config --global user.email 'you@example.com'",
        )

    hook_path = root / ".githooks" / "post-merge"
    hook_content = ""
    if hook_path.exists():
        hook_content = hook_path.read_text(encoding="utf-8", errors="replace")

    # CHECK 2 — post-merge hook installed
    if hook_path.exists() and "teamcache sync" in hook_content:
        add("PASS", "post-merge hook installed", ".githooks/post-merge contains teamcache sync")
    else:
        add(
            "WARN",
            "post-merge hook installed",
            ".githooks/post-merge is not installed or does not run teamcache sync",
            "Run: teamcache init --enable-hooks",
        )

    # CHECK 3 — hook binary path valid
    teamcache_bin = ""
    for line in hook_content.splitlines():
        if "teamcache" not in line or "sync" not in line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        if "sync" in tokens:
            sync_index = tokens.index("sync")
            if sync_index > 0:
                teamcache_bin = tokens[sync_index - 1]
                break
        for token in tokens:
            if Path(token).name.startswith("teamcache"):
                teamcache_bin = token
                break
        if teamcache_bin:
            break
    resolved_bin = Path(teamcache_bin).expanduser() if teamcache_bin else None
    binary_exists = False
    if resolved_bin and resolved_bin.exists():
        binary_exists = True
    elif teamcache_bin and shutil.which(teamcache_bin):
        binary_exists = True
    if binary_exists:
        add("PASS", "hook binary path valid", teamcache_bin)
    else:
        add(
            "FAIL",
            "hook binary path valid",
            "teamcache binary referenced by .githooks/post-merge was not found",
            "Run: teamcache init --enable-hooks to reinstall",
        )

    # CHECK 4 — v1 objects present when schema is v2
    v1_schema_objects = 0
    if SCHEMA_VERSION == "v2":
        for path in (root / SUMMARIES_DIR).glob("**/*_v1.json"):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if obj.get("schema_version") == "v1":
                v1_schema_objects += 1
    if v1_schema_objects:
        add(
            "WARN",
            "v1 objects present when schema is v2",
            f"{v1_schema_objects} v1 summary object(s) found",
            "Run: teamcache migrate",
        )
    else:
        add("PASS", "v1 objects present when schema is v2", "no stale v1 summary objects found")

    # CHECK 5 — embeddings DB healthy
    embeddings_path = root / EMBEDDINGS_DB
    if embeddings_path.exists():
        try:
            emb_conn = sqlite3.connect(embeddings_path)
            try:
                count = emb_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            finally:
                emb_conn.close()
            add("PASS", "embeddings DB healthy", f"{count} embeddings")
        except (OSError, sqlite3.DatabaseError):
            add(
                "WARN",
                "embeddings DB healthy",
                ".teamcache/local/embeddings.sqlite could not be read",
                "Delete .teamcache/local/embeddings.sqlite and run teamcache sync",
            )
    else:
        add("PASS", "embeddings DB healthy", "embeddings DB does not exist")

    # CHECK 6 — index DB healthy
    index_path = root / INDEX_DB
    if index_path.exists():
        try:
            index_conn = sqlite3.connect(index_path)
            try:
                count = index_conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            finally:
                index_conn.close()
            add("PASS", "index DB healthy", f"{count} summaries")
        except (OSError, sqlite3.DatabaseError):
            add(
                "WARN",
                "index DB healthy",
                ".teamcache/local/index.sqlite could not be read",
                "Delete .teamcache/local/index.sqlite and run teamcache sync",
            )
    else:
        add(
            "WARN",
            "index DB healthy",
            ".teamcache/local/index.sqlite does not exist",
            "Run: teamcache sync",
        )

    status_markup = {
        "PASS": "[green]PASS[/green]",
        "WARN": "[yellow]WARN[/yellow]",
        "FAIL": "[red]FAIL[/red]",
    }
    passed = 0
    for status, name, detail, fix in results:
        if status == "PASS":
            passed += 1
        console.print(f"{status_markup[status]} {name}: {escape(detail)}")
        if fix:
            console.print(f"  Fix: {escape(fix)}")
    console.print(f"{passed}/6 checks passed")


def _pick_summary_winner(
    a: dict[str, object],
    b: dict[str, object],
) -> dict[str, object]:
    """Return the preferred summary object between two candidates.

    Preference order:
    1. ai beats static
    2. longer ai summary beats shorter ai summary
    3. otherwise keep a (ours)
    """
    a_type = a.get("summary_type", "static")
    b_type = b.get("summary_type", "static")

    if a_type == "ai" and b_type != "ai":
        return a
    if b_type == "ai" and a_type != "ai":
        return b
    if a_type == "ai" and b_type == "ai":
        a_len = len(str(a.get("summary", "")))
        b_len = len(str(b.get("summary", "")))
        return a if a_len >= b_len else b
    return a


_SUPPORTED_AGENTS = ("claude", "codex", "cursor", "opencode", "aider", "windsurf")


@cli.command()
@click.option(
    "--agent",
    default="claude",
    show_default=True,
    type=click.Choice(_SUPPORTED_AGENTS),
    help="AI tool to register with.",
)
@click.option("--dry-run", is_flag=True, help="Show what would change without writing files.")
@click.option("--print-diff", is_flag=True, help="Print a unified diff of file changes.")
def install(agent: str, dry_run: bool, print_diff: bool) -> None:
    try:
        repo_root = find_repo_root(Path.cwd())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    teamcache_bin = _resolve_teamcache_bin()
    _agent_install(agent, repo_root, teamcache_bin, dry_run=dry_run, print_diff=print_diff)


@cli.command()
@click.option(
    "--agent",
    default="claude",
    show_default=True,
    type=click.Choice(_SUPPORTED_AGENTS),
    help="AI tool to unregister from.",
)
@click.option("--dry-run", is_flag=True, help="Show what would change without writing files.")
@click.option("--print-diff", is_flag=True, help="Print a unified diff of file changes.")
def uninstall(agent: str, dry_run: bool, print_diff: bool) -> None:
    try:
        repo_root = find_repo_root(Path.cwd())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _agent_uninstall(agent, repo_root, dry_run=dry_run, print_diff=print_diff)


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


def _agent_install(
    agent: str,
    repo_root: Path,
    teamcache_bin: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if agent == "claude":
        if not dry_run and not shutil.which("claude"):
            raise click.ClickException("'claude' not found in PATH")
        if not dry_run:
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
        _append_claude_instructions(repo_root / "CLAUDE.md", dry_run=dry_run, print_diff=print_diff)
        console.print("Claude Code: MCP registered, CLAUDE.md updated.")

    elif agent == "codex":
        mcp_config = repo_root / ".codex" / "config.json"
        _write_mcp_json(mcp_config, str(teamcache_bin), dry_run=dry_run, print_diff=print_diff)
        _append_instructions_block(
            repo_root / "AGENTS.md",
            _AGENTS_INSTRUCTIONS_BLOCK,
            dry_run=dry_run,
            print_diff=print_diff,
        )
        console.print("Codex: .codex/config.json written, AGENTS.md updated.")

    elif agent == "cursor":
        mcp_config = repo_root / ".cursor" / "mcp.json"
        _write_mcp_json(mcp_config, str(teamcache_bin), dry_run=dry_run, print_diff=print_diff)
        _append_instructions_block(
            repo_root / ".cursorrules",
            _CURSORRULES_BLOCK,
            dry_run=dry_run,
            print_diff=print_diff,
        )
        console.print("Cursor: .cursor/mcp.json written, .cursorrules updated.")

    elif agent == "opencode":
        mcp_config = repo_root / ".opencode" / "config.json"
        _write_mcp_json(mcp_config, str(teamcache_bin), dry_run=dry_run, print_diff=print_diff)
        _append_instructions_block(
            repo_root / "AGENTS.md",
            _AGENTS_INSTRUCTIONS_BLOCK,
            dry_run=dry_run,
            print_diff=print_diff,
        )
        console.print("OpenCode: .opencode/config.json written, AGENTS.md updated.")

    elif agent == "aider":
        # Aider has no MCP; inject instructions via --read config
        instructions_path = repo_root / ".teamcache-instructions.md"
        _write_text_change(
            instructions_path,
            _AIDER_INSTRUCTIONS_FILE,
            dry_run=dry_run,
            print_diff=print_diff,
        )
        _write_aider_config(
            repo_root / ".aider.conf.yml",
            ".teamcache-instructions.md",
            dry_run=dry_run,
            print_diff=print_diff,
        )
        console.print("Aider: .teamcache-instructions.md written, .aider.conf.yml updated.")

    elif agent == "windsurf":
        mcp_config = repo_root / ".windsurf" / "mcp.json"
        _write_mcp_json(mcp_config, str(teamcache_bin), dry_run=dry_run, print_diff=print_diff)
        _append_instructions_block(
            repo_root / ".windsurfrules",
            _CURSORRULES_BLOCK,
            dry_run=dry_run,
            print_diff=print_diff,
        )
        console.print("Windsurf: .windsurf/mcp.json written, .windsurfrules updated.")


def _agent_uninstall(
    agent: str,
    repo_root: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if agent == "claude":
        if not dry_run and shutil.which("claude"):
            try:
                subprocess.run(
                    ["claude", "mcp", "remove", "teamcache", "--scope", "project"],
                    cwd=repo_root,
                    check=False,
            )
            except OSError:
                pass
        _remove_instructions_block(repo_root / "CLAUDE.md", dry_run=dry_run, print_diff=print_diff)
        console.print("Claude Code: MCP removed, CLAUDE.md cleaned.")

    elif agent == "codex":
        _remove_mcp_json(repo_root / ".codex" / "config.json", dry_run=dry_run, print_diff=print_diff)
        _remove_instructions_block(repo_root / "AGENTS.md", dry_run=dry_run, print_diff=print_diff)
        console.print("Codex: config removed.")

    elif agent == "cursor":
        _remove_mcp_json(repo_root / ".cursor" / "mcp.json", dry_run=dry_run, print_diff=print_diff)
        _remove_instructions_block(repo_root / ".cursorrules", dry_run=dry_run, print_diff=print_diff)
        console.print("Cursor: config removed.")

    elif agent == "opencode":
        _remove_mcp_json(repo_root / ".opencode" / "config.json", dry_run=dry_run, print_diff=print_diff)
        _remove_instructions_block(repo_root / "AGENTS.md", dry_run=dry_run, print_diff=print_diff)
        console.print("OpenCode: config removed.")

    elif agent == "aider":
        _remove_aider_config(
            repo_root / ".aider.conf.yml",
            ".teamcache-instructions.md",
            dry_run=dry_run,
            print_diff=print_diff,
        )
        instructions = repo_root / ".teamcache-instructions.md"
        _delete_file_change(instructions, dry_run=dry_run, print_diff=print_diff)
        console.print("Aider: config removed.")

    elif agent == "windsurf":
        _remove_mcp_json(repo_root / ".windsurf" / "mcp.json", dry_run=dry_run, print_diff=print_diff)
        _remove_instructions_block(repo_root / ".windsurfrules", dry_run=dry_run, print_diff=print_diff)
        console.print("Windsurf: config removed.")


_TEAMCACHE_START = "<!-- TEAMCACHE:START -->"
_TEAMCACHE_END = "<!-- TEAMCACHE:END -->"

_AGENTS_INSTRUCTIONS_BLOCK = """\
<!-- TEAMCACHE:START -->
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
<!-- TEAMCACHE:END -->
"""

_CURSORRULES_BLOCK = """\
<!-- TEAMCACHE:START -->
# TeamCache
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path).
After reading any file (when not ai-cached): call cache_summary(file_path, summary, language).
Before any task: call find_relevant_files(task_description).
<!-- TEAMCACHE:END -->
"""

_AIDER_INSTRUCTIONS_FILE = """\
# TeamCache Instructions
At session start: call repo_overview() for a map of the repo.
Before reading any file: call get_file_context(file_path).
After reading any file (when not ai-cached): call cache_summary(file_path, summary, language).
Before any task: call find_relevant_files(task_description).
"""


def _write_mcp_json(
    config_path: Path,
    teamcache_bin: str,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    import json as _json
    existing: dict[str, object] = {}
    if config_path.exists():
        try:
            existing = _json.loads(config_path.read_text(encoding="utf-8"))
        except _json.JSONDecodeError as exc:
            raise click.ClickException(
                f"malformed JSON in {config_path}; refusing to modify"
            ) from exc
        if not isinstance(existing, dict):
            raise click.ClickException(f"JSON config in {config_path} must be an object")
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise click.ClickException(f"mcpServers in {config_path} must be an object")
    servers["teamcache"] = {"command": teamcache_bin, "args": ["serve"]}
    _write_text_change(
        config_path,
        _json.dumps(existing, indent=2) + "\n",
        dry_run=dry_run,
        print_diff=print_diff,
    )


def _remove_mcp_json(
    config_path: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    import json as _json
    if not config_path.exists():
        return
    try:
        data = _json.loads(config_path.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        raise click.ClickException(
            f"malformed JSON in {config_path}; refusing to modify"
        ) from exc
    if not isinstance(data, dict):
        raise click.ClickException(f"JSON config in {config_path} must be an object")
    servers = data.get("mcpServers")
    if servers is None:
        return
    if not isinstance(servers, dict):
        raise click.ClickException(f"mcpServers in {config_path} must be an object")
    if "teamcache" not in servers:
        return
    servers.pop("teamcache")
    _write_text_change(
        config_path,
        _json.dumps(data, indent=2) + "\n",
        dry_run=dry_run,
        print_diff=print_diff,
    )


def _append_instructions_block(
    path: Path,
    block: str,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8")
        newline = "\r\n" if "\r\n" in content else "\n"
    else:
        content = ""
        newline = os.linesep
    updated = _upsert_teamcache_block(content, block.replace("\n", newline), newline)
    _write_text_change(path, updated, dry_run=dry_run, print_diff=print_diff)


def _remove_instructions_block(
    path: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    updated = _remove_teamcache_blocks(content)
    if updated == content:
        return
    _write_text_change(path, updated, dry_run=dry_run, print_diff=print_diff)


def _teamcache_block_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    search_from = 0
    while True:
        start = content.find(_TEAMCACHE_START, search_from)
        next_end = content.find(_TEAMCACHE_END, search_from)
        if next_end != -1 and (start == -1 or next_end < start):
            raise click.ClickException("incomplete TeamCache marker block; refusing to modify")
        if start == -1:
            return spans
        end = content.find(_TEAMCACHE_END, start + len(_TEAMCACHE_START))
        if end == -1:
            raise click.ClickException("incomplete TeamCache marker block; refusing to modify")
        end += len(_TEAMCACHE_END)
        if content.startswith("\r\n", end):
            end += 2
        elif content.startswith("\n", end) or content.startswith("\r", end):
            end += 1
        spans.append((start, end))
        search_from = end


def _upsert_teamcache_block(content: str, block: str, newline: str) -> str:
    spans = _teamcache_block_spans(content)
    if not spans:
        return content + block

    first_start, first_end = spans[0]
    suffix_parts: list[str] = []
    previous_end = first_end
    for start, end in spans[1:]:
        suffix_parts.append(content[previous_end:start])
        previous_end = end
    suffix_parts.append(content[previous_end:])
    return content[:first_start] + block + "".join(suffix_parts)


def _remove_teamcache_blocks(content: str) -> str:
    spans = _teamcache_block_spans(content)
    if not spans:
        return content
    updated = content
    for start, end in reversed(spans):
        updated = updated[:start] + updated[end:]
    return updated


def _write_text_change(
    path: Path,
    content: str,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if before == content:
        return
    if print_diff:
        _print_text_diff(path, before, content)
    if dry_run:
        return
    if path.exists():
        _create_teamcache_backup(path)
    _atomic_write_text(path, content)


def _print_text_diff(path: Path, before: str, after: str) -> None:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
    )
    diff_text = "".join(diff)
    if diff_text:
        click.echo(diff_text, nl=False)


def _create_teamcache_backup(path: Path) -> None:
    backup_path = path.with_name(path.name + ".teamcache.bak")
    backup_path.write_bytes(path.read_bytes())


def _delete_file_change(
    path: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if not path.exists():
        return
    before = path.read_text(encoding="utf-8")
    if print_diff:
        _print_text_diff(path, before, "")
    if dry_run:
        return
    _create_teamcache_backup(path)
    path.unlink()


def _write_aider_config(
    config_path: Path,
    instructions_rel: str,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        if instructions_rel in content:
            return
        newline = "\r\n" if "\r\n" in content else "\n"
        prefix = content
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        _write_text_change(
            config_path,
            prefix + f"read:\n  - {instructions_rel}\n".replace("\n", newline),
            dry_run=dry_run,
            print_diff=print_diff,
        )
    else:
        _write_text_change(
            config_path,
            f"read:\n  - {instructions_rel}\n",
            dry_run=dry_run,
            print_diff=print_diff,
        )


def _remove_aider_config(
    config_path: Path,
    instructions_rel: str,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    if not config_path.exists():
        return
    import re as _re
    content = config_path.read_text(encoding="utf-8")
    updated = _re.sub(rf"\n?\s*-\s*{_re.escape(instructions_rel)}\n?", "\n", content)
    _write_text_change(config_path, updated, dry_run=dry_run, print_diff=print_diff)


def _index_file(
    root: Path,
    schema_version: str,
    conn,
    emb_conn,
    path: Path,
    created_by: str,
    store,
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
    disk_symbol_obj = store.read_symbol(key)
    symbols = (
        disk_symbol_obj.get("symbols", {})
        if isinstance(disk_symbol_obj, dict)
        else extract_symbols(path, language, text)
    )
    if not isinstance(symbols, dict):
        symbols = extract_symbols(path, language, text)
    summary = summary_from_symbols(symbols, language)
    created_at = datetime.utcnow().isoformat() + "Z"
    disk_summary_obj = store.read(key)
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
            store.write(key, summary_obj)
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
            store.write_symbol(key, symbol_obj)
        insert_symbol(conn, symbol_obj)
    return {
        "skipped": False,
        "language": language,
        "symbol_obj": symbol_obj,
    }


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
    reverse_imports: dict[str, list[str]] = {}
    for obj in symbol_objects:
        symbols = obj.get("symbols", {})
        if not isinstance(symbols, dict):
            continue
        importer = str(obj.get("file_path", ""))
        if not importer:
            continue
        for import_name in symbols.get("imports", []):
            for lookup_key in _import_lookup_keys(str(import_name)):
                resolved_path, _ = definition_locations.get(lookup_key, ("", ""))
                if resolved_path and resolved_path != importer:
                    reverse_imports.setdefault(resolved_path, [])
                    if importer not in reverse_imports[resolved_path]:
                        reverse_imports[resolved_path].append(importer)
                    break

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
        "reverse_imports": reverse_imports,
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


def _append_claude_instructions(
    path: Path,
    *,
    dry_run: bool = False,
    print_diff: bool = False,
) -> None:
    block = """<!-- TEAMCACHE:START -->
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
<!-- TEAMCACHE:END -->
"""
    _append_instructions_block(path, block, dry_run=dry_run, print_diff=print_diff)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
