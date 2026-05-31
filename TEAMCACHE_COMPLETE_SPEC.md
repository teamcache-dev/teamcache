# TeamCache — Complete Problem Statement & Solution Approach
# All 4 Phases

---

## The Core Problem

Software teams using AI coding assistants on large codebases 
burn their monthly token budget on one repeated activity — 
understanding the same codebase from scratch, independently, 
every single session, every single developer.

10 developers. Same files. Read every day. Nobody shares 
what they learned. Budget exhausted in 10-15 working days 
instead of 20-22.

**Root cause in one sentence:**
Claude Code has no concept of shared understanding across 
team members. Every session is an island.

---

## The Solution in One Sentence

Parse every file instantly with static analysis to eliminate 
the cold start. When an AI tool reads a file, have it upgrade 
that static summary to a rich one. Share everything via git. 
Every developer after the first pays near zero tokens for 
files that have not changed.

---

## Architecture Principles (All Phases)

These never change across any phase:

```
1. CLI first — pip install, works everywhere, no GUI needed

2. stdio MCP only — no ports, no servers, no infrastructure
   The AI tool spawns teamcache as subprocess

3. Immutable objects — summaries never updated, only created
   Old ones become irrelevant when file changes

4. Git as sync — .teamcache/objects/ committed to repo
   Team shares understanding via normal git workflow

5. SQLite local only — fast index, never committed,
   never loaded into Claude context, rebuilt from objects

6. No external AI calls — teamcache never calls any AI API
   No API keys. No provider config. No separate cost.
   The AI tool already running writes summaries via cache_summary()

7. Two-tier summaries — every file always has at least a
   static summary from day one. AI tools upgrade to rich
   summaries as they read files. No cold start.

8. Cache key formula:
   file_hash  = sha256(file_content_bytes)
   cache_key  = sha256(file_hash + "|" + schema_version)
   File changes → new file_hash → new cache_key → old object ignored.
```

---

## Folder Structure (Permanent)

```
your-repo/
  .teamcache/
    objects/                  ← GIT COMMITTED
      summaries/
        ab/
          ab3f9b2c1d4e_v1.json   ← immutable summary objects
        c2/
          c2a1d9e3f5b7_v1.json
      symbols/                ← Phase 2
        ab/
          ab3f9b2c1d4e_v1.json
      repomap.json            ← Phase 2
    config.yaml               ← GIT COMMITTED
    local/                    ← GITIGNORED
      index.sqlite            ← fast lookup, rebuilt from objects
      embeddings.sqlite       ← Phase 2
  .gitignore                  ← .teamcache/local/ added here
  CLAUDE.md                   ← teamcache block added here
```

---

## Summary Object Format (Permanent)

```json
{
  "cache_key": "sha256_of_file_hash_plus_schema_version",
  "file_path": "src/auth/middleware.py",
  "file_hash": "sha256_of_file_content_only",
  "summary": "imports: tokens, config | classes: AuthMiddleware | functions: validate_token, raise_auth_error",
  "summary_type": "static",
  "schema_version": "v1",
  "created_at": "2026-05-31T10:00:00Z",
  "created_by": "git-user-email",
  "file_size_bytes": 4821,
  "language": "python"
}
```

`summary_type` is either:
- `"static"` — parsed from source structure, no AI involved
- `"ai"` — written by the AI tool via cache_summary(), richer and more useful

When an AI tool upgrades a static summary to an AI summary,
a new object is written with the same cache_key but 
summary_type: "ai". The static object remains (immutable).
SQLite index always points to the best available object 
(ai preferred over static).

---

# PHASE 1 — Core
## "Make it work"

### Problem This Phase Solves

Developers read files they have already read. No tool 
intercepts this. No understanding is shared. Every session 
starts from zero.

### What Gets Built

**4 CLI commands:**

```
teamcache init
teamcache index
teamcache serve
teamcache install
```

**4 MCP tools exposed:**

```
repo_overview()
get_file_context(file_path)
find_relevant_files(task)
cache_summary(file_path, summary, language)
```

### Command Detail

**teamcache init**
```
- Check current directory is a git repo (warn if not)
- Create .teamcache/objects/summaries/
- Create .teamcache/local/
- Write .teamcache/config.yaml (schema_version only)
- Add .teamcache/local/ to .gitignore (idempotent — check before appending)
- Print next steps: "Run teamcache index, then teamcache install"
```

No AI provider. No API key. Nothing to configure.

**teamcache index (static only — no AI)**
```
- Walk all repo files
- Skip: node_modules, .git, dist, build,
        __pycache__, *.lock, *.pyc,
        binary files (extension + null-byte check, no libmagic),
        files over 500KB
- For each file:
    compute cache_key
    check SQLite: ai summary exists at this cache_key? → skip
    check SQLite: static summary exists at this cache_key? → skip
    NO → run static parser for this language
         write JSON object (summary_type: "static")
         insert into SQLite index
- Show progress bar via Rich
- Print: X files parsed, Y skipped (already indexed)
- Runs in seconds. No network. No API key.
```

Static parser extracts per file:
```
Python:
  imports:   all import / from...import statements
  classes:   class names (top-level)
  functions: def names (top-level)

JavaScript / TypeScript:
  imports:   import / require statements
  exports:   export declarations
  classes:   class names
  functions: function names, arrow functions assigned to const

Go:
  package:   package name
  imports:   import paths
  functions: func names
  types:     type...struct names

Other languages:
  best-effort: extract lines matching common def/class/func patterns

Output format (stored as summary string):
  "imports: tokens, config | classes: AuthMiddleware | functions: validate_token, raise_auth_error"
```

**teamcache serve (stdio MCP)**
```
repo_overview() → string
  Returns:
  - Directory tree (2 levels deep, skips node_modules/.git/etc)
    Uses os.walk with dirs[:] pruning, not rglob
  - Languages detected (by file extension count)
  - Total files, files with ai summary, files with static summary
  - Entry points guessed (main.py, index.js, main.go, src/main.rs,
    Makefile, Dockerfile, index.ts, app.py, server.py)

get_file_context(file_path) → dict
  Three possible responses:

  1. AI summary exists (best):
     { summary: "...", summary_type: "ai", cached: true,
       file_path, language,
       note: "Read exact source before editing" }

  2. Static summary exists (good enough for routing):
     { summary: "imports: x | classes: Y | functions: z",
       summary_type: "static", cached: true,
       file_path, language,
       note: "Static index only. Read file for full understanding,
              then call cache_summary() with what you learned." }

  3. Nothing cached (cold file, index not run yet):
     { summary: null, summary_type: null, cached: false,
       file_path, language,
       note: "Not indexed. Read file normally, then call cache_summary()." }

  Path safety: resolve to absolute path, verify inside repo root
  using path.is_relative_to(root) with both sides resolved.

cache_summary(file_path, summary, language) → dict
  Called by the AI tool after it reads and understands a file.
  Always writes summary_type: "ai" — this is the upgrade path.
  
  - Resolve file_path relative to repo root
  - Verify path is inside repo root
  - Read file bytes → compute file_hash and cache_key
  - If file does not exist: return {stored: false, error: "file not found"}
  - If ai summary already exists at this cache_key:
      return {stored: false, reason: "ai summary already cached"}
  - Write JSON object (summary_type: "ai") to
      objects/summaries/{cache_key[:2]}/{cache_key[:12]}_v1.json
    (atomic write: write to .tmp then os.replace())
  - Upsert into SQLite index (ai preferred over static)
  - Return {stored: true, cache_key: "...", summary_type: "ai"}

find_relevant_files(task) → list
  SQLite FTS5 full-text search across all summaries
  (both static and ai summaries searchable)
  Returns top 5: [{file_path, summary, summary_type, language}]
  AI summaries ranked above static summaries on equal score
```

**teamcache install**
```
- Detect Claude Code (check for 'claude' in PATH)
- Resolve full path to teamcache executable (sys.executable-relative)
  to handle venv installations correctly
- Run: claude mcp add teamcache --scope project -- <full-path> serve
- Append to CLAUDE.md (idempotent — skip if sentinel line present):

  ## TeamCache
  At session start: call repo_overview() for a map of the repo.
  Before reading any file: call get_file_context(file_path)
    - summary_type "ai"     → use summary, skip reading (unless editing)
    - summary_type "static" → use for navigation, read file for full
                              understanding, then call cache_summary()
    - cached false          → read file normally, then call cache_summary()
  After reading any file: call cache_summary(file_path, summary, language)
    where summary is your own understanding of the file in plain text.
    Summaries are shared with the whole team via git.
  Before any task: call find_relevant_files(task_description)

- Confirm to user
```

### How the Cache Grows (No Cold Start)

```
Day 0 — setup
  teamcache init
  teamcache index        ← static parse entire repo in seconds
                           every file now has a structural summary
  teamcache install
  git commit .teamcache/objects/   ← whole team gets static summaries

Session 1 — Developer A
  calls get_file_context("src/auth/middleware.py")
  → summary_type: "static", cached: true
  → reads summary: "imports: tokens, config | classes: AuthMiddleware..."
  → decides to open the file for the full picture
  → reads the file (would have done this anyway for a static summary)
  → calls cache_summary("src/auth/middleware.py",
      "Handles JWT authentication. Validates tokens on every request.
       Raises AuthError on failure. Depends on tokens.py and config.py.",
      "python")
  → teamcache writes a new object (summary_type: "ai")
  → developer commits (including .teamcache/objects/)

Session 2 — Developer B (after git pull)
  calls get_file_context("src/auth/middleware.py")
  → summary_type: "ai", cached: true
  → reads summary: "Handles JWT authentication..."
  → done. Never touches the raw file. Tokens saved.
```

Hot files get AI summaries fast (first person who reads them).
All files have static summaries from day one (no cold start).
Static summaries are good enough for `find_relevant_files` routing.
AI summaries accumulate over time, replacing static ones.

### config.yaml Contents

```yaml
schema_version: v1
```

That is all. No provider, no model, no API key.

### Binary File Detection (No libmagic)

```python
BINARY_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf',
                     '.zip', '.tar', '.gz', '.exe', '.dll', '.so',
                     '.dylib', '.wasm', '.bin', '.dat', '.db', ...}

def is_binary(path):
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        chunk = path.read_bytes()[:4096]
        return b'\x00' in chunk
    except OSError:
        return True
```

No python-magic. No libmagic DLL. Works on all platforms.

### Dependencies Phase 1

```toml
mcp>=1.0.0
click>=8.0.0
pyyaml>=6.0
rich>=13.0.0
```

No anthropic. No openai. No python-magic.

### Success Criteria Phase 1

```
- teamcache index parses a real repo in under 30 seconds
- Every file has at least a static summary after teamcache index
- get_file_context returns static summary immediately (no cold start)
- Claude Code calls cache_summary after reading uncached or static files
- cache_summary writes a valid AI summary object atomically
- A second developer gets summary_type: "ai" for files Developer A read
- No API key required at any point
- find_relevant_files returns useful results using static summaries alone
```

---

# PHASE 2 — Smarter
## "Make it accurate"

### Problem This Phase Solves

Phase 1 static summaries are structural but shallow. AI summaries 
are richer but only exist for files developers have actually read.
find_relevant_files works but semantic search would be far better.
Cache invalidation is manual.

Phase 2 adds deeper structural understanding and automatic freshness.

### What Gets Built

**3 new commands:**

```
teamcache changed
teamcache sync
teamcache invalidate
```

**3 new MCP tools:**

```
get_symbols(file_path)
find_by_symbol(symbol_name)
get_changed_context(since_branch)
```

**New internals:**

```
Tree-sitter symbol extraction (replaces regex static parser)
Git hook for auto-invalidation
Semantic search via local embeddings
```

### Command Detail

**teamcache changed --since main**
```
- Run git diff --name-only main...HEAD
- For each changed file:
    invalidate existing summaries (mark old objects stale in SQLite)
    re-run static parser → write new static summary object
    extract symbol index via tree-sitter
- Show: X files changed, Y summaries refreshed
  (AI summaries regenerate organically as AI tool reads those files)
```

**teamcache sync**
```
- Rebuild local SQLite index from all objects in .teamcache/objects/
- Use when teammate pushed new cache objects
- Run automatically on git pull via hook
```

**teamcache invalidate**
```
teamcache invalidate file_path    ← single file
teamcache invalidate --stale      ← anything over 30 days
teamcache invalidate --all        ← nuclear option
- Marks entries invalid in SQLite
- Does not delete objects (immutable)
- teamcache index re-generates static summary on next run
- AI tool re-generates AI summary on next read
```

**Symbol extraction (internal — replaces Phase 1 regex parser)**
```
- Use tree-sitter for: Python, JS, TS, Java, Go, Rust, C, C++
- Extract per file:
    functions (name, line, signature)
    classes (name, line, methods)
    imports (what this file depends on)
    exports (what this file provides)
- Store as objects/symbols/{prefix}/{key}.json
- Static summary string now generated from tree-sitter output
  (more accurate than Phase 1 regex)
```

**Semantic search (internal)**
```
- Local embedding model: all-MiniLM-L6-v2 (80MB, CPU)
- No API calls for search
- find_relevant_files now uses semantic similarity
  instead of FTS5 keyword matching
- Works well on both static and AI summaries
```

**Git hook (auto-invalidation)**
```
.githooks/post-merge:
  CHANGED=$(git diff --name-only ORIG_HEAD HEAD)
  teamcache invalidate --files $CHANGED --quiet
  teamcache sync --quiet

teamcache init installs this automatically
git config core.hooksPath .githooks
```

### New MCP Tools Phase 2

```
get_symbols(file_path) → dict
  Returns all functions, classes, imports, exports
  From tree-sitter symbol index, not raw file
  Sub-millisecond response

find_by_symbol(symbol_name) → list
  "where is UserService defined"
  Returns file paths and line numbers
  No file reading required

get_changed_context(since_branch) → dict
  What changed since main
  Which AI summaries were invalidated
  Which files now have only static summaries again
  Call this at session start automatically
```

### Success Criteria Phase 2

```
- find_relevant_files returns correct files 80%+ of time
- Git hook invalidates stale summaries automatically
- teamcache changed --since main runs in under 10 seconds
- Symbol lookup works for Python, JS, TS at minimum
- No manual cache management needed by developers
```

---

# PHASE 3 — Team
## "Make it shared"

### Problem This Phase Solves

Phase 2 works well for one developer. Sharing still requires 
remembering to commit .teamcache/objects/. Coverage is invisible 
— nobody knows which files still have only static summaries vs 
rich AI summaries. Teams cannot see who is contributing.

Phase 3 makes sharing automatic and coverage visible.

### What Gets Built

**3 new commands:**

```
teamcache commit
teamcache stats
teamcache report
```

**CI integration:**

```
GitHub Actions workflow
GitLab CI template
```

**Team dashboard:**

```
Terminal-based stats view
Per-developer contribution breakdown
Static vs AI summary coverage tracking
```

### Command Detail

**teamcache commit**
```
- git add .teamcache/objects/
- git commit -m "chore: update teamcache [$(date)]"
- One command instead of two
```

**Post-commit hook (auto-commit)**
```
.githooks/post-commit:
  CACHE_CHANGES=$(git status --porcelain .teamcache/objects/)
  if [ -n "$CACHE_CHANGES" ]; then
    git add .teamcache/objects/
    git commit --amend --no-edit --quiet
  fi
```

**teamcache stats**
```
Output:
┌──────────────────────────────────────────┐
│ TeamCache Stats — Last 30 Days           │
├──────────────────────────────────────────┤
│ Files total:         2,103               │
│ AI summaries:          847  (40%)        │
│ Static summaries:    1,156  (55%)        │
│ Not indexed:           100   (5%)        │
│ Cache hit rate:         68%              │
│ Top contributors (AI summaries):         │
│   dev1@team.com    → 312 objects         │
│   dev2@team.com    → 287 objects         │
│   dev3@team.com    → 198 objects         │
│ Most-read uncached:                      │
│   src/core/engine.py   (static only)     │
│   src/api/routes.py    (static only)     │
└──────────────────────────────────────────┘
```

**teamcache report**
```
- Generates markdown report
- AI vs static summary coverage breakdown
- Hottest files still on static summaries (prime targets to read)
- Cache hit rate trend over time
- Per-developer AI summary contributions
- Exports to .teamcache/reports/YYYY-MM.md
```

**CI workflow (GitHub Actions)**
```yaml
name: TeamCache Sync
on:
  push:
    branches: [main]
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install teamcache
      - run: teamcache sync
      - run: teamcache invalidate --stale
      - run: teamcache index          # re-run static parser on any new files
      - run: |
          git add .teamcache/objects/
          git diff --staged --quiet || git commit -m "chore: teamcache sync [skip ci]"
          git push
```

No API key in CI. CI runs static index for new files, clears stale 
entries, and commits. AI summaries come from developers' sessions.

### Success Criteria Phase 3

```
- Zero developer action needed to share cache
- Stats show AI vs static coverage clearly
- CI keeps static index fresh on every merge to main
- AI summary coverage above 50% of hot files after 2 weeks
- No API key ever required anywhere in the system
```

---

# PHASE 4 — Distribution
## "Make it available to everyone"

### Problem This Phase Solves

Phase 3 works for your team. Phase 4 makes it available to 
every team in the world in 30 seconds.

### What Gets Built

**PyPI release:**
```
pip install teamcache
uv tool install teamcache
pipx install teamcache
```

**npm wrapper:**
```
npx teamcache@latest init
npm install -g teamcache
```

**Single binary:**
```
curl -fsSL https://install.teamcache.dev | sh
# No Python required
# Works on Mac, Linux, Windows
# ~15MB self-contained binary (no AI deps)
```

**Documentation site:**
```
teamcache.dev
- Getting started (5 minutes)
- How it works (two-tier summaries explained)
- Configuration reference
- MCP tools reference
- Troubleshooting
```

**Multi-agent support:**
```
teamcache install --agent claude    ← Claude Code
teamcache install --agent codex     ← OpenAI Codex CLI
teamcache install --agent cursor    ← Cursor
teamcache install --agent opencode  ← OpenCode
teamcache install --agent aider     ← Aider
teamcache install --agent windsurf  ← Windsurf

Each agent gets the correct config format and instructions
for that tool's equivalent of CLAUDE.md.
```

**Security hardening:**
```
Secret scanner before any cache_summary() write
30+ regex patterns (AWS keys, API tokens, etc.)
Files with secrets silently excluded from cache
Never commit sensitive content accidentally
```

### Release Checklist Phase 4

```
- PyPI package builds cleanly
- npm wrapper installs Python package correctly
- Single binary builds for Mac ARM, Mac Intel, Linux x64, Windows
- All 6 agents tested with teamcache install
- Documentation covers all commands and all 4 MCP tools
- README has quickstart under 10 steps
- CI tests run on push
- Version pinning works correctly
- Uninstall command works cleanly
```

### Success Criteria Phase 4

```
- Any developer can install in under 30 seconds
- Works on Mac, Linux, Windows without Python knowledge
- 6 AI agents supported
- 100 external teams using it within first month
- Zero data leaving developer network
- Zero API keys required at any point
```

---

# Summary Table

| Phase | Name | Duration | Delivers |
|---|---|---|---|
| 1 | Core | Week 1-2 | init, index (static), serve, install — 4 MCP tools, no cold start, no API key |
| 2 | Smarter | Week 3-4 | tree-sitter symbols, semantic search, auto-invalidation |
| 3 | Team | Week 5-6 | auto-sharing, coverage stats, CI integration |
| 4 | Distribution | Week 7-8 | PyPI, npm, binary, docs, 6 agents |

---
