# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in editable mode with dev deps
pip install -e ".[dev]"

# Install optional extras (tree-sitter symbols + semantic search)
pip install -e ".[all]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_cli_install.py

# Run a single test by name
pytest tests/test_cli_install.py::test_repeated_install_is_idempotent

# Run tests with coverage
pytest --cov=teamcache

# Run the CLI directly
python -m teamcache --help
```

### CLI command reference

| Command | Purpose |
|---|---|
| `teamcache init [--enable-hooks]` | Create `.teamcache/` dirs, update `.gitignore`, optionally install post-merge hook |
| `teamcache index` | Walk `git ls-files`, generate static summaries, write object files, rebuild local DB |
| `teamcache sync` | Rebuild local SQLite index from committed object files (run after `git pull`) |
| `teamcache changed [--since main]` | Re-index only files changed since a branch |
| `teamcache install [--agent claude\|codex\|cursor\|opencode\|aider\|windsurf]` | Register MCP server + inject instruction block into agent config |
| `teamcache uninstall [--agent ...]` | Reverse of install |
| `teamcache commit` | Stage and commit `.teamcache/objects/` (aborts if you have other staged changes) |
| `teamcache stats` / `teamcache metrics` | Coverage and performance stats |
| `teamcache report` | Write a markdown report to `.teamcache/reports/YYYY-MM.md` |
| `teamcache invalidate <file>` / `--stale` / `--all` | Remove entries from local index |
| `teamcache doctor` | Health check: git identity, hook, binary path, DB integrity |
| `teamcache migrate` | Apply pending SQLite migrations to local index |
| `teamcache migrate-hooks` | Remove dangerous `--amend` lines left by v0.1.x post-commit hook |
| `teamcache merge-driver %O %A %B` | Git merge driver for `.teamcache/objects/` JSON (prefers ai > static, longer wins) |
| `teamcache serve` | Launch MCP stdio server (used internally by agent config) |

## Architecture

TeamCache is a Python CLI + MCP stdio server. It gives AI tools shared, git-committed file summaries so every developer after the first pays near-zero tokens to understand files that haven't changed.

### Core concepts

**Cache key:** `sha256(sha256(file_bytes) + "|" + schema_version)`. File changes → new key → old object ignored automatically. No explicit invalidation needed for correctness.

**Two-tier summaries:**
- `static` — generated from regex/tree-sitter parsing, milliseconds, no AI, written on `teamcache index`
- `ai` — written by the AI tool via `cache_summary()` MCP call after it reads a file; richer, takes precedence over static in all queries

**Immutable objects:** once written, `.teamcache/objects/**/*.json` files are never modified. `ai` summaries for the same cache key overwrite the SQLite index but the static object file stays. The naming scheme is `{key[:2]}/{key[:12]}_v1.json`.

**Local vs committed:**
- `.teamcache/objects/` — committed to git, shared with the team
- `.teamcache/local/` — gitignored, rebuilt locally from objects via `teamcache sync`

### Module map

| File | Responsibility |
|---|---|
| `teamcache/cli.py` | All CLI commands (`init`, `index`, `install`, `uninstall`, `sync`, `changed`, `invalidate`, `stats`, `report`, `commit`, `serve`). Also owns install/uninstall logic for all 6 agents. |
| `teamcache/mcp_server.py` | MCP stdio server; exposes `repo_overview`, `get_file_context`, `cache_summary`, `find_relevant_files`, `get_symbols`, `find_by_symbol`, `get_changed_context`, `get_dependents`, `get_audit_log` |
| `teamcache/db.py` | SQLite schema and all queries. Two tables (`summaries`, `symbols`) each with an FTS5 virtual table (`fts_summaries`, `fts_symbols`). WAL mode. Schema migrated in-place on connect. |
| `teamcache/files.py` | File iteration (`iter_repo_files` — always uses `git ls-files` for safety), binary detection, static regex summary extraction, `cache_key`/`file_hash` computation, sensitive path denylist, `.teamcacheignore` support |
| `teamcache/symbols.py` | Tree-sitter symbol extraction (falls back to regex without the optional `symbols` extra). Writes symbol objects to `.teamcache/objects/symbols/`. |
| `teamcache/secrets.py` | 30+ regex patterns to detect credentials before `cache_summary()` writes. If a match is found, the summary is rejected silently. |
| `teamcache/search.py` | FTS query normalisation; semantic search via `sentence-transformers` (optional `embeddings` extra, falls back to keyword search) |
| `teamcache/embeddings.py` | `embeddings.sqlite` management for semantic vector search |
| `teamcache/store.py` | `ObjectStore` protocol + `GitObjectStore` (read/write content-addressed JSON in `.teamcache/objects/`). `make_store(config, repo_root)` is the factory; only `"git"` backend exists today. |
| `teamcache/config.py` | `TeamCacheConfig` dataclass + `find_repo_root`, `load_config`, `write_config`. Config fields: `schema_version`, `scope_paths`, `objects_backend`, `objects_backend_url`, `sensitive_path_denylist`, and others. |
| `teamcache/constants.py` | `SKIP_DIRS`, `BINARY_EXTENSIONS`, `SKIP_SUFFIXES`, `LANGUAGE_BY_SUFFIX`, path constants |

### Install/uninstall mechanics

`teamcache install --agent <name>` writes two things per agent:
1. An MCP config file (e.g. `.cursor/mcp.json`, `.codex/config.json`) or runs `claude mcp add` for Claude Code.
2. A fenced instruction block delimited by `<!-- TEAMCACHE:START -->` / `<!-- TEAMCACHE:END -->` appended to the agent's instruction file (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, etc.).

The block is idempotent (upserted by `_upsert_teamcache_block`). Before modifying any existing file a `.teamcache.bak` copy is created. All file writes are atomic (write-to-`.tmp` then `os.replace`).

### MCP tools

`find_relevant_files` and `get_changed_context` are `async def` — FastMCP handles async tools natively. Both fire concurrent `git log` calls via `asyncio.gather` to fetch per-file last-modified timestamps. `repo_overview` has a 60-second in-process response cache (`_OVERVIEW_CACHE`). The server opens two SQLite connections in WAL mode: `read_conn` for all queries, `write_conn` for all mutations.

| Tool | What it returns |
|---|---|
| `repo_overview()` | Directory tree, language breakdown, summary coverage, entry points (60 s cache) |
| `get_file_context(file_path)` | Cached summary + `summary_type` ("ai"\|"static"), `summary_confidence` (high/medium/low by age), `quality_score`, moved-file note |
| `cache_summary(file_path, summary, language)` | Writes an AI summary object to disk and DB; validates length, blocks sensitive paths and secrets |
| `find_relevant_files(task)` | async — merges semantic + FTS keyword search, gathers git mtimes, sorts by `quality_score` DESC |
| `get_symbols(file_path)` | Returns extracted symbol table (functions, classes, imports) for a file |
| `find_by_symbol(symbol_name)` | Finds which file defines a symbol |
| `get_changed_context(since_branch)` | async — git diff via async subprocess, gathers git mtimes per changed file |
| `get_dependents(file_path)` | Returns files that import a given file, resolved from `repomap.json` |
| `get_audit_log(file_path?, limit?)` | Returns recent write events from the `audit_log` table |

### Key invariants

- **`ObjectStore` protocol** (`store.py`): all disk writes for summary/symbol objects go through `GitObjectStore.write` / `write_symbol`. Never call `write_summary_object` or `write_symbol_object` from `files.py`/`symbols.py` directly in new code — those remain as the underlying implementations only.
- **`db.py` migrations** are append-only integers in `MIGRATIONS`. The current highest is 5 (`quality_score` column). Add new ones at the end; never renumber.
- **`quality_score`** is computed by `compute_quality_score(summary, file_size_bytes)` in `db.py` and stored in `summaries`. `insert_summary` always recomputes it on write. Old rows from before migration 5 get `0.5` (the SQL DEFAULT).
- **`scope_paths`** in `TeamCacheConfig`: if non-empty, `teamcache index` filters the file list to matching prefixes and `find_relevant_files` filters search results the same way.
- **SQLite connections in `mcp_server.py`**: always use `read_conn` for `get_*`/`keyword_search`/`count_by_type`/`find_symbol_by_name` and `write_conn` for `insert_*`/`write_audit_event`/`optimize_fts`/`rebuild_*`. Both are closed in the `finally` block of `run_server`.

### Testing patterns

Tests use `click.testing.CliRunner` and `tmp_path`/`monkeypatch` fixtures. `subprocess.run` calls (for `claude mcp add` and `git`) are monkeypatched in install tests. No real filesystem side-effects outside `tmp_path`.
