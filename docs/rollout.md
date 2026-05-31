# TeamCache Rollout Guide

This guide covers rolling out TeamCache to a team, reviewing what gets committed, recovering from problems, setting up CI, and upgrading from v0.1.x.

---

## Table of Contents

1. [Safe Order of Operations](#1-safe-order-of-operations)
2. [Reviewing Indexed Content Before Committing](#2-reviewing-indexed-content-before-committing)
3. [Rollback Instructions](#3-rollback-instructions)
4. [CI Usage](#4-ci-usage)
5. [Upgrading from v0.1.x](#5-upgrading-from-v01x)

---

## 1. Safe Order of Operations

Follow these steps in order. Each step is safe to re-run.

### Step 1 — Install TeamCache

```bash
pip install teamcache
```

Confirm the install:

```bash
teamcache --version
```

### Step 2 — Initialize in your repository

Run this from the root of your git repository:

```bash
cd your-repo
teamcache init
```

What this does:

- Creates `.teamcache/objects/summaries/` and `.teamcache/objects/symbols/`
- Creates `.teamcache/local/` (local SQLite indexes, never committed)
- Writes `.teamcache/config.yaml` with `schema_version: v1`
- Appends `.teamcache/local/` to `.gitignore`

To also install the post-merge git hook that auto-invalidates stale summaries after `git pull`:

```bash
teamcache init --enable-hooks
```

### Step 3 — Build the static index

```bash
teamcache index
```

This walks every file in the repository and generates a structural summary for each one using static analysis (tree-sitter where available, regex fallback otherwise). No AI. No API key. Completes in seconds even on large repositories.

Output looks like:

```
Indexing ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%
2103 files indexed, 47 skipped (already current)
```

Files are skipped if a current summary already exists at the same cache key.

### Step 4 — Review what will be committed

Before sharing the index with your team, verify the contents (see [Section 2](#2-reviewing-indexed-content-before-committing)).

### Step 5 — Register with your AI tool

```bash
teamcache install                        # Claude Code (default)
teamcache install --agent cursor         # Cursor
teamcache install --agent codex          # OpenAI Codex CLI
teamcache install --agent windsurf       # Windsurf
teamcache install --agent aider          # Aider
teamcache install --agent opencode       # OpenCode
```

For Claude Code, this runs `claude mcp add teamcache --scope project` and appends instructions to `CLAUDE.md`. For other agents it writes the appropriate MCP config file and instruction file for that tool.

### Step 6 — Commit the static index

```bash
git add .teamcache/
git commit -m "chore: add teamcache static index"
git push
```

After this commit, every teammate who pulls the branch immediately has structural summaries for every file. There is no cold start.

### Step 7 — Each teammate installs TeamCache locally

Every developer on the team runs:

```bash
pip install teamcache
teamcache install          # or --agent <their-tool>
```

They do not need to run `teamcache init` or `teamcache index` again. The committed objects are already present. When they open the repository their AI tool calls `repo_overview()` and immediately has structural context.

### Step 8 — AI summaries accumulate automatically

As developers use their AI tools, the tools call `cache_summary()` after reading files. This writes richer AI summaries into `.teamcache/objects/`. Each developer commits their new summary objects periodically:

```bash
teamcache commit
# equivalent to: git add .teamcache/objects/ && git commit
```

After a few days, the most active files have AI summaries. After a few weeks, hot files have rich AI summaries that every team member benefits from without reading the raw files.

---

## 2. Reviewing Indexed Content Before Committing

TeamCache only indexes structural content derived from your source files. It never reads secrets from environment, configuration, or CI variables. However, it is good practice to review what will be committed before sharing with your team.

### Check coverage

```bash
teamcache stats
```

This shows how many files have AI summaries vs static summaries, which files are most active, and per-developer contribution counts.

### Inspect a specific summary

Summary objects are plain JSON files inside `.teamcache/objects/summaries/`. Each file name starts with the first two characters of the cache key:

```
.teamcache/objects/summaries/
  ab/
    ab3f9b2c1d4e_v1.json
  c2/
    c2a1d9e3f5b7_v1.json
```

Open any file to read the summary text:

```json
{
  "cache_key": "ab3f9b2c1d4e...",
  "file_path": "src/auth/middleware.py",
  "file_hash": "...",
  "summary": "imports: tokens, config | classes: AuthMiddleware | functions: validate_token, raise_auth_error",
  "summary_type": "static",
  "schema_version": "v1",
  "created_at": "2026-05-31T10:00:00Z",
  "created_by": "dev@example.com",
  "file_size_bytes": 4821,
  "language": "python"
}
```

The `summary` field is what gets shared. For `summary_type: "static"` it is a structural parse (imports, class names, function names). For `summary_type: "ai"` it is prose written by the AI tool after reading the file.

### Check for secrets before committing

TeamCache ships a built-in secret scanner that runs before every `cache_summary()` write. Files matching common secret patterns are silently excluded. To manually check what is staged:

```bash
git diff --staged .teamcache/objects/
```

Look for any `summary` fields that appear to contain tokens, keys, passwords, or other credentials. If found, run:

```bash
teamcache invalidate <path-to-source-file>
```

Then re-run `teamcache index` to regenerate a clean static summary for that file, and verify again before committing.

### Check which files were skipped

`teamcache index` skips binary files, files over 500 KB, and common generated directories (`node_modules`, `dist`, `build`, `__pycache__`, `.git`). If you expect a file to appear in the index and it does not:

```bash
teamcache stats
```

Files listed under "Not indexed" are either excluded by default rules or have not been read by an AI tool yet.

---

## 3. Rollback Instructions

These steps remove TeamCache completely from a repository. Run them in order.

### Step 1 — Unregister from your AI tool

```bash
teamcache uninstall
```

For a specific agent:

```bash
teamcache uninstall --agent cursor
teamcache uninstall --agent codex
# etc.
```

This removes the MCP registration and cleans the instructions block from `CLAUDE.md` (or the equivalent file for other agents).

### Step 2 — Remove the cached objects from git tracking

```bash
git rm -r .teamcache/
```

This stages all `.teamcache/` contents for removal. If you want to keep the directory locally but stop tracking it:

```bash
git rm -r --cached .teamcache/
```

### Step 3 — Restore the default git hooks path

If you installed git hooks via `teamcache init --enable-hooks`, the repository's `core.hooksPath` was set to `.githooks`. Reset it to the git default:

```bash
git config --unset core.hooksPath
```

### Step 4 — Remove the hooks directory

```bash
rm -rf .githooks/
```

On Windows PowerShell:

```powershell
Remove-Item -Recurse -Force .githooks
```

### Step 5 — Commit the removal

```bash
git add -A
git commit -m "chore: remove teamcache"
git push
```

### Complete rollback — single sequence

```bash
teamcache uninstall
git rm -r .teamcache/
git config --unset core.hooksPath
rm -rf .githooks/
git add -A
git commit -m "chore: remove teamcache"
git push
```

After pushing, teammates who pull will have the `.teamcache/` directory removed from their working tree. They should also run `teamcache uninstall` locally if they installed the MCP registration.

---

## 4. CI Usage

TeamCache CI jobs run the static indexer to keep summaries fresh on every merge to the main branch. They do not require an API key and do not generate AI summaries — those come from developer sessions.

### GitHub Actions

The workflow file is included at `.github/workflows/teamcache-sync.yml`. It triggers on every push to `main`, runs the static indexer over any new files, removes summaries older than 30 days, and commits the result.

```yaml
name: TeamCache Sync

on:
  push:
    branches: [main]

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install teamcache
        run: pip install teamcache

      - name: Sync index from committed objects
        run: teamcache sync

      - name: Remove stale summaries
        run: teamcache invalidate --stale

      - name: Index any new files
        run: teamcache index

      - name: Commit updated cache objects
        run: |
          git config user.email "github-actions@github.com"
          git config user.name "github-actions"
          git add .teamcache/objects/
          git diff --staged --quiet || git commit -m "chore: teamcache sync [skip ci]"
          git push
```

The `[skip ci]` marker in the commit message prevents the workflow from triggering itself recursively.

The `contents: write` permission is required for the commit and push step. No other permissions are needed.

### GitLab CI

The template file is at `.gitlab/teamcache-sync.yml`. Include it in your main pipeline:

```yaml
include:
  - local: .gitlab/teamcache-sync.yml
```

Or copy the job definition directly:

```yaml
teamcache-sync:
  stage: .post
  image: python:3.11-slim
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  before_script:
    - pip install --quiet teamcache
  script:
    - teamcache sync
    - teamcache invalidate --stale
    - teamcache index
    - |
      git config user.email "ci@gitlab"
      git config user.name "GitLab CI"
      git add .teamcache/objects/
      git diff --staged --quiet || git commit -m "chore: teamcache sync [skip ci]"
    - git push "https://oauth2:${CI_JOB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" HEAD:${CI_COMMIT_BRANCH}
```

The job runs in the `.post` stage so it executes after your normal pipeline stages. `CI_JOB_TOKEN` is provided automatically by GitLab — no additional secrets are required.

### What CI does and does not do

CI runs:

- `teamcache sync` — rebuilds the local SQLite index from committed objects
- `teamcache invalidate --stale` — marks summaries older than 30 days as requiring refresh
- `teamcache index` — generates static summaries for any files not yet in the index

CI does not generate AI summaries. AI summaries are written by developer sessions via `cache_summary()` and committed by developers. CI only maintains the static layer.

---

## 5. Upgrading from v0.1.x

v0.1.x installed a `post-commit` hook in `.githooks/post-commit` that contained a `git commit --amend` line. This line was removed in v0.1.4 because amending a commit in a hook is dangerous — it can silently rewrite commits and cause push failures.

### Check if you are affected

```bash
cat .githooks/post-commit
```

If the file contains a line with `--amend`, you are affected.

### Migrate with one command

```bash
teamcache migrate-hooks
```

This removes any `--amend` lines from `.githooks/post-commit` and leaves the rest of the hook intact. The post-merge hook (which syncs the index after `git pull`) is not touched.

Output when migration is needed:

```
Removed 3 amend line(s) from .githooks/post-commit.
```

Output when already clean:

```
No amend lines found. Hook is already clean.
```

### What to do if init warns about the old hook

If you run `teamcache init` on a repository that was set up with v0.1.x, you will see:

```
warning: .githooks/post-commit exists from a previous install.
Run: teamcache migrate-hooks
```

Run `teamcache migrate-hooks` to clear the warning on future `init` calls.

### Upgrade checklist

1. Upgrade the package:

   ```bash
   pip install --upgrade teamcache
   ```

2. Run the hook migration in every repository where TeamCache was previously installed:

   ```bash
   teamcache migrate-hooks
   ```

3. Commit the updated hook file:

   ```bash
   git add .githooks/post-commit
   git commit -m "chore: remove amend from teamcache post-commit hook"
   git push
   ```

4. Notify teammates to pull and run `pip install --upgrade teamcache` locally. They do not need to run `migrate-hooks` again once the hook file is updated in the repository.

5. Optionally re-run `teamcache init --enable-hooks` to install the updated post-merge hook if it was not present in the v0.1.x installation.
