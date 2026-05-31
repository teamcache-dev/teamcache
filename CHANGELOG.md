# Changelog

All notable changes to teamcache will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
