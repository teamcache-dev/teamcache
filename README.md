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
| `teamcache init [--enable-hooks]` | Initialize in the current git repo; optionally install post-merge hook |
| `teamcache index` | Parse all files, build static summaries and symbol index |
| `teamcache install [--agent NAME] [--dry-run] [--print-diff]` | Register MCP server with your AI tool |
| `teamcache serve` | Start the MCP stdio server (called by AI tool) |
| `teamcache changed [--since BRANCH]` | Re-index files changed since a branch |
| `teamcache sync` | Rebuild local index from committed objects |
| `teamcache invalidate [PATH\|--stale\|--all]` | Mark entries as needing refresh |
| `teamcache check-cached FILE` | Check if file has an AI summary (exits 0 if yes, 1 if no) |
| `teamcache session-uncached` | List files read by AI this session that lack an AI summary |
| `teamcache pr-check [--since BRANCH]` | Verify that files changed since a branch have AI summaries |
| `teamcache stats` | Show AI vs static coverage, top contributors |
| `teamcache metrics [--format json] [--since DATE]` | Cache performance metrics |
| `teamcache report` | Write `.teamcache/reports/YYYY-MM.md` |
| `teamcache commit` | `git add .teamcache/objects/ && git commit` |
| `teamcache uninstall [--agent NAME] [--dry-run] [--print-diff]` | Remove MCP registration and instructions |
| `teamcache doctor` | Health check: git identity, hook, binary path, DB integrity |
| `teamcache migrate` | Apply pending SQLite schema migrations |
| `teamcache migrate-hooks` | Remove legacy `--amend` lines from old post-commit hook |
| `teamcache merge-driver %O %A %B` | Git merge driver for `.teamcache/objects/` — wires up in `.gitattributes` |

## MCP tools

Your AI tool gets these tools via the MCP server:

| Tool | What it does |
|---|---|
| `repo_overview()` | Directory tree, languages, entry points, coverage (60 s cached) |
| `get_file_context(path)` | Returns AI or static summary, confidence tier, quality score; tells AI what to do next |
| `cache_summary(path, summary, lang)` | AI writes its understanding back into the cache |
| `find_relevant_files(task)` | Semantic + keyword search across all summaries, ranked by quality score |
| `get_symbols(path)` | Functions, classes, imports for a file |
| `get_file_symbols(path)` | Functions and classes with exact line numbers and signatures — use before `get_symbol_source()` |
| `get_symbol_source(path, symbol_name)` | Returns only the source lines for one named function or method, without loading the whole file |
| `find_by_symbol(name)` | Where is `UserService` defined? Line number included. |
| `get_changed_context(branch)` | What changed since main, which need AI re-read, with last-modified timestamps |
| `get_dependents(path)` | Files that import **or call into** a given file, with a `via` field (`"import"`, `"call"`, or `"import+call"`) |
| `get_audit_log(path?, limit?)` | Recent `cache_summary` write and eviction history |

## Architecture

```
.teamcache/
  objects/            ← git committed — shared with team
    summaries/        ← AI and static summary objects (immutable JSON, schema v2)
    symbols/          ← tree-sitter symbol index objects (schema vs1, versioned separately)
    repomap.json      ← cross-file import map + call graph
  config.yaml         ← schema_version, scope_paths, objects_backend, and other settings
  local/              ← gitignored — rebuilt locally
    index.sqlite      ← fast lookup index (WAL mode)
    embeddings.sqlite ← semantic search vectors
```

**Cache keys:** summaries and symbol objects use separate, independently-versioned cache keys:
- Summary key: `sha256(sha256(file_bytes) + "|" + SCHEMA_VERSION)` (schema `v2`)
- Symbol key: `sha256(sha256(file_bytes) + "|" + SYMBOL_SCHEMA_VERSION)` (schema `vs1`)

File changes → new key → old object ignored automatically. The separate versioning lets symbol objects be upgraded without invalidating existing AI summaries.

**Two-tier summaries:**
- `static` — tree-sitter parse, runs in milliseconds, no AI, available from day one
- `ai` — written by the AI tool after it reads a file, much richer, preferred when available

**Symbol objects** (schema `vs1`) store, per file:
- `functions` — name, line, end_line, signature, calls (outgoing call names)
- `classes` — name, line, methods (name, line, end_line)
- `imports` — import/include/use statements
- `outgoing_calls` — deduplicated list of all callee names across all functions in the file (at top level, not inside `symbols`, so the FTS index stays clean)

**Repomap** (`.teamcache/objects/repomap.json`) is built in three passes:
1. **Pass 1** — language counts, definition locations for all symbols and module aliases
2. **Pass 2a** — `reverse_imports` (which files import a given file), built after Pass 1 so all definitions are resolved
3. **Pass 2b** — `call_graph` (which files call into a given file at symbol level), derived from `outgoing_calls`
4. **Pass 3** — `top_symbols` (most-imported symbols across the repo)

**Quality score:** every summary gets a `quality_score` (0–1) computed from word count and specificity. `find_relevant_files` ranks results by this score so richer summaries surface first.

**Scope paths:** set `scope_paths` in `.teamcache/config.yaml` to restrict indexing and search to specific directory prefixes — useful for monorepos where a team only owns part of the tree.

**No external calls for core indexing.** The AI tool already running writes summaries via `cache_summary()`. teamcache never calls any AI API. No API key. No separate cost. Semantic search (optional, disabled by default) downloads an ~80 MB embedding model from HuggingFace on first use.

## Supported languages

tree-sitter provides full AST-level symbol extraction for:

| Language | Extensions | Symbols extracted |
|---|---|---|
| Python | `.py` | functions, classes, methods, imports |
| JavaScript | `.js`, `.jsx` | functions, classes, methods, imports |
| TypeScript | `.ts`, `.tsx` | functions, classes, methods, imports |
| Go | `.go` | functions, types, imports |
| Java | `.java` | functions, classes, methods, imports |
| Rust | `.rs` | functions, types, imports |
| C# | `.cs` | functions, classes, methods, imports |
| Ruby | `.rb` | functions, classes, methods, imports |
| Kotlin | `.kt` | functions, classes, methods, imports |
| PHP | `.php` | functions, classes, imports |
| **C / C++** | `.c`, `.h`, `.cpp`, `.cc`, `.hpp` | functions, classes/structs/unions, methods, `#include`, `using` |

All other text-based file types fall back to a fast regex-based extractor.

## What gets committed to git

Everything under `.teamcache/objects/` is committed to your repository and shared with your team. This includes:

- **Static summaries** — structural snapshots of each file (symbols, imports, file shape), generated by `teamcache index` with no AI involved.
- **Symbol objects** — per-file symbol tables (functions with line spans and outgoing calls, classes with methods), stored separately from summaries under `objects/symbols/`.
- **AI summaries** — richer descriptions written by your AI tool via `cache_summary()` after it reads a file. These contain whatever the AI chose to write, which may include excerpts or paraphrases of your source code.

Because these objects are immutable and content-addressed, old summaries accumulate in git history even after the source files change. To audit what is stored:

```bash
# List all summary objects committed to the repo
git ls-files .teamcache/objects/summaries/

# List all symbol objects
git ls-files .teamcache/objects/symbols/

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
- `tree-sitter` + language grammars — full AST symbol extraction for 11 languages including C/C++ (falls back to regex without it)
- `sentence-transformers` — semantic search (falls back to keyword search without it); downloads an ~80 MB model from HuggingFace on first use

## License

MIT
