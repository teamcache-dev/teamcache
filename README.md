# TeamCache

Shared AI context cache for software teams. Eliminates the cold start — every developer after the first pays near-zero tokens for files that haven't changed.

## The problem

10 developers. Same files. Read by AI every day. Nobody shares what was learned. Monthly token budget exhausted in 10–15 days instead of 20–22.

## How it works

1. **Static index** — `teamcache index` parses every file in seconds with tree-sitter. No AI, no API key. Every file gets a structural summary immediately.
2. **AI upgrade** — When your AI tool reads a file, it calls `cache_summary()` to store a richer understanding. That summary is shared with your whole team via git.
3. **No cold start** — The next developer gets the AI summary instantly. They never read the raw file. Tokens saved.

---

## Quickstart (under 10 steps)

```bash
# 1. Install
pip install teamcache

# 2. Go to your repo
cd your-repo

# 3. Initialize
teamcache init

# 4. Index the whole repo (seconds, no AI, no API key)
teamcache index

# 5. Register with your AI tool
teamcache install                        # Claude Code (default)
teamcache install --agent cursor         # Cursor
teamcache install --agent codex          # OpenAI Codex CLI
teamcache install --agent windsurf       # Windsurf
teamcache install --agent aider          # Aider
teamcache install --agent opencode       # OpenCode

# 6. Commit the static index to share with your team
git add .teamcache/objects/
git commit -m "chore: add teamcache static index"
git push
```

That's it. Your AI tool now calls `get_file_context()` before reading any file and `cache_summary()` after. Every teammate gets the benefit.

---

## Commands

| Command | What it does |
|---|---|
| `teamcache init` | Initialize in the current git repo |
| `teamcache index` | Parse all files, build static summaries |
| `teamcache install [--agent NAME]` | Register MCP server with your AI tool |
| `teamcache serve` | Start the MCP stdio server (called by AI tool) |
| `teamcache changed [--since BRANCH]` | Re-index files changed since a branch |
| `teamcache sync` | Rebuild local index from committed objects |
| `teamcache invalidate [PATH\|--stale\|--all]` | Mark entries as needing refresh |
| `teamcache stats` | Show AI vs static coverage, top contributors |
| `teamcache report` | Write `.teamcache/reports/YYYY-MM.md` |
| `teamcache commit` | `git add .teamcache/objects/ && git commit` |
| `teamcache uninstall [--agent NAME]` | Remove MCP registration and instructions |

## MCP tools

Your AI tool gets these tools via the MCP server:

| Tool | What it does |
|---|---|
| `repo_overview()` | Directory tree, languages, entry points, coverage |
| `get_file_context(path)` | Returns AI or static summary; tells AI what to do |
| `cache_summary(path, summary, lang)` | AI writes its understanding back into the cache |
| `find_relevant_files(task)` | Semantic + keyword search across all summaries |
| `get_symbols(path)` | Functions, classes, imports for a file |
| `find_by_symbol(name)` | Where is `UserService` defined? Line number included. |
| `get_changed_context(branch)` | What changed since main, which need AI re-read |

## Architecture

```
.teamcache/
  objects/          ← git committed — shared with team
    summaries/      ← AI and static summary objects (immutable JSON)
    symbols/        ← tree-sitter symbol index objects
    repomap.json    ← cross-file import map
  config.yaml       ← schema_version: v1 (nothing else)
  local/            ← gitignored — rebuilt locally
    index.sqlite    ← fast lookup index
    embeddings.sqlite ← semantic search vectors
```

**Cache key:** `sha256(sha256(file_bytes) + "|" + schema_version)`  
File changes → new key → old object ignored automatically.

**Two-tier summaries:**
- `static` — tree-sitter parse, runs in milliseconds, no AI, available from day one
- `ai` — written by the AI tool after it reads a file, much richer, preferred when available

**No external calls from teamcache itself.** The AI tool already running writes summaries via `cache_summary()`. teamcache never calls any AI API. No API key. No separate cost.

## CI integration

### GitHub Actions

`.github/workflows/teamcache-sync.yml` is included — keeps static index fresh on every merge to main.

### GitLab CI

See `.gitlab/teamcache-sync.yml`. Include it in your pipeline:

```yaml
include:
  - local: .gitlab/teamcache-sync.yml
```

## Requirements

- Python 3.10+
- Git
- No API key required at any point

Optional (installed automatically with `pip install teamcache[all]`):
- `tree-sitter` — more accurate symbol extraction (falls back to regex without it)
- `sentence-transformers` — semantic search (falls back to keyword search without it)

## License

MIT
