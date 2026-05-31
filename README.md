# TeamCache

Shared AI context cache for software teams. Reduces cold start — every developer after the first gets reduced token usage for files with AI summaries. Static summaries reduce cold-start cost; full savings require AI summaries to exist.

## The problem

10 developers. Same files. Read by AI every day. Nobody shares what was learned. Monthly token budget exhausted in 10–15 days instead of 20–22.

## How it works

1. **Static index** — `teamcache index` parses every file in seconds with tree-sitter. No AI, no API key. Every file gets a structural summary immediately.
2. **AI upgrade** — When your AI tool reads a file, it calls `cache_summary()` to store a richer understanding. That summary is shared with your whole team via git.
3. **Reduced cold start** — The next developer gets the AI summary instantly. They never read the raw file. Tokens saved.

> **Honest limitations**
>
> - The **static index** (built by `teamcache index`) gives structural context only: file shapes, symbols, imports. It does not capture intent, patterns, or logic.
> - The **AI tier** requires that at least one developer has already read the file with a supported AI tool and had `cache_summary()` called. New files and new repos start with static summaries only.
> - **No external AI calls** — teamcache itself never calls any AI API. The AI tool you are already using writes summaries back via `cache_summary()`. However, the optional semantic search feature downloads an ~80 MB embedding model from HuggingFace on first use (disabled by default; enabled with `pip install teamcache[all]`).

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
teamcache install --dry-run --print-diff # Preview file changes

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
| `teamcache install [--agent NAME] [--dry-run] [--print-diff]` | Register MCP server with your AI tool |
| `teamcache serve` | Start the MCP stdio server (called by AI tool) |
| `teamcache changed [--since BRANCH]` | Re-index files changed since a branch |
| `teamcache sync` | Rebuild local index from committed objects |
| `teamcache invalidate [PATH\|--stale\|--all]` | Mark entries as needing refresh |
| `teamcache stats` | Show AI vs static coverage, top contributors |
| `teamcache report` | Write `.teamcache/reports/YYYY-MM.md` |
| `teamcache commit` | `git add .teamcache/objects/ && git commit` |
| `teamcache uninstall [--agent NAME] [--dry-run] [--print-diff]` | Remove MCP registration and instructions |

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

**No external calls for core indexing.** The AI tool already running writes summaries via `cache_summary()`. teamcache never calls any AI API. No API key. No separate cost. Semantic search (optional, disabled by default) downloads an ~80 MB embedding model from HuggingFace on first use.

## What gets committed to git

Everything under `.teamcache/objects/` is committed to your repository and shared with your team. This includes:

- **Static summaries** — structural snapshots of each file (symbols, imports, file shape), generated by `teamcache index` with no AI involved.
- **AI summaries** — richer descriptions written by your AI tool via `cache_summary()` after it reads a file. These contain whatever the AI chose to write, which may include excerpts or paraphrases of your source code.

Because these objects are immutable and content-addressed, old summaries accumulate in git history even after the source files change. To audit what is stored:

```bash
# List all summary objects committed to the repo
git ls-files .teamcache/objects/summaries/

# Search summary content for a string (e.g. a token or hostname)
grep -r "search_term" .teamcache/objects/summaries/
```

If you need to remove a summary from history entirely, use `git filter-repo` or `BFG Repo-Cleaner` — a normal `git rm` will not expunge it from older commits.

---

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
- `sentence-transformers` — semantic search (falls back to keyword search without it); downloads an ~80 MB model from HuggingFace on first use

## License

MIT
