# Changelog

All notable changes to teamcache will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-06-04

### New Features

- `teamcache/store.py`: pluggable `ObjectStore` protocol with `GitObjectStore` implementation; `make_store()` factory wired into CLI and MCP server — all object reads/writes go through the store layer
- `scope_paths` config field: restrict indexing and `find_relevant_files` results to specific directory prefixes (useful for monorepos)
- `quality_score` on every summary (0–1, stored in DB, migration 5): blend of word-count length score and capitalised-word specificity score; `find_relevant_files` ranks by this score
- `get_dependents(file_path)` MCP tool: returns files that import a given file, resolved from `repomap.json`
- `get_audit_log(file_path?, limit?)` MCP tool: returns recent write and eviction events from the `audit_log` table
- `_repo_overview()` now reads `repomap.json` for language counts, Top Modules, and Entry Points instead of running `os.walk` on every call; 60-second response cache added
- `find_relevant_files` and `get_changed_context` converted to `async def` with concurrent `git log` calls via `asyncio.gather` for per-file last-modified timestamps
- Staleness check in `find_relevant_files`: computes `stale`, `file_last_modified`, and `summary_age_days` per result; sorts stale results after fresh ones
- `_GIT_MTIME_CACHE` session cache prevents redundant `git log` subprocess calls
- Separate `read_conn` / `write_conn` SQLite connections in MCP server (WAL mode)
- `teamcache doctor` command: 6-point health check (git identity, hook, binary path, schema objects, embeddings DB, index DB)
- `teamcache metrics` command: cache performance dashboard with `--format json` and `--since DATE` options
- `teamcache merge-driver`: git merge driver for `.teamcache/objects/` JSON files (AI beats static, longer wins)
- Cross-file dependency tracking: `_write_repomap()` builds `reverse_imports` map; `get_dependents` MCP tool queries it
- Kotlin and PHP tree-sitter extractors added to `symbols.py`
- C# and Ruby tree-sitter extractors added to `symbols.py`
- `objects_backend` and `objects_backend_url` config fields (reserved for future non-git backends)
- GitHub Actions workflow (`.github/workflows/teamcache-pr.yml`): comments on PRs when changed files lack AI summaries

### Fixes

- `teamcache install` and `teamcache uninstall` are now non-destructive (idempotent upsert/remove of instruction blocks)

### Migration from v0.2.0

Run after pulling:

```
teamcache sync
teamcache migrate
```

`teamcache sync` rebuilds the local index from committed objects. `teamcache migrate` applies DB migrations 3–5 (daily_stats, audit_log, quality_score).

## [0.2.0] - 2026-05-31

### Breaking Changes

- `teamcache init` no longer installs a post-commit hook (removed dangerous `git commit --amend` behavior)
- `teamcache init` no longer sets `core.hooksPath` by default (hooks are now opt-in)
- File indexing now uses `git ls-files` instead of `os.walk` (only git-tracked files are indexed)
- Embeddings are now disabled by default (set `enable_embeddings: true` to enable)

### Security Fixes

- **P0-A**: Removed post-commit hook that ran `git commit --amend` (could corrupt rebase history)
- **P0-B**: Hooks are now opt-in via `--enable-hooks` flag with conflict detection
- **P0-C**: `iter_repo_files()` now uses `git ls-files`, never indexes gitignored files
- **P0-C**: Added hardcoded sensitive path denylist (`.env`, `*.pem`, `.ssh/`, etc.)
- **P0-D**: Fixed shell injection in post-merge hook (filenames with spaces)

### New Features

- `teamcache init --enable-hooks` flag for explicit hook installation
- `teamcache init --force-hooks` to override hook conflicts
- `teamcache migrate-hooks` command to clean up v0.1.x post-commit hooks
- `.teamcacheignore` file support for additional exclusions
- `--stdin` flag for `teamcache invalidate`

### Improvements

- **P1-A**: Stale eviction now uses `last_accessed_at` instead of `created_at`
- **P1-B**: `get_file_context()` AI summaries now include stronger "MUST read source" note
- **P1-C**: Embedding count guard prevents loading all embeddings into RAM
- **P1-D**: Embeddings disabled by default (no HuggingFace download on fresh install)
- **P1-E**: Moved file detection in `get_file_context()` (`path_mismatch` flag)
- **P2-B**: `repo_overview()` language counts use `git ls-files`
- **P2-C**: `teamcache commit` checks for staged changes before adding cache objects

### Migration from v0.1.x

Run the following command to migrate from v0.1.x:

```
teamcache migrate-hooks
```

This removes dangerous amend lines from any existing `.githooks/post-commit` file.
