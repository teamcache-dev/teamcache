# Security

This document describes the threat model, protective controls, and known limitations for TeamCache.

---

## Threat model

TeamCache is a **local developer tool**. It runs on your machine, reads files from your git working tree, and writes summary objects back into a `.teamcache/objects/` directory that your team shares via git.

**What TeamCache never does:**

- It never transmits files, source code, or summaries to any external server.
- It never calls an AI API or any third-party service. The AI tool you already have running (Claude Code, Cursor, etc.) calls `cache_summary()` to store summaries; TeamCache itself makes no outbound network requests.
- It never reads files outside the repository root.

**What TeamCache does do:**

- It writes summary objects to `.teamcache/objects/`, which are committed to git and shared with your team. A summary is a short, structured description of a file (imports, classes, functions). If source code containing credentials were accidentally summarised, that summary would be stored in git history.

The protective controls below are designed to prevent that from happening.

---

## Hardcoded path denylist

TeamCache maintains a hardcoded denylist of sensitive path patterns. Files matching any of these patterns are **never read, indexed, or summarised**, regardless of any other configuration. This denylist cannot be overridden or disabled.

| Pattern | What it covers |
|---|---|
| `.env`, `.env.*` | Environment variable files |
| `*.pem`, `*.key` | PEM certificates and private keys |
| `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.ppk` | Key stores and credential bundles |
| `.ssh/` | SSH directory and all its contents |
| `.aws/` | AWS credentials and config directory |
| `.gnupg/` | GnuPG keyring directory |
| `id_rsa`, `id_ecdsa`, `id_ed25519`, `id_dsa` | SSH private key files |
| `.netrc` | Netrc credentials file |
| `.npmrc` | npm authentication tokens |
| `.pypirc` | PyPI upload credentials |
| `secrets/`, `credentials/` | Common credential directories |

The match is case-insensitive and applies to any path component, so `config/.env.production` and `subdir/.ssh/id_rsa` are both blocked.

---

## Adding additional exclusions via .teamcacheignore

Place a `.teamcacheignore` file in your repository root to exclude additional paths from indexing. The format is identical to `.gitignore`: one glob pattern per line, with `#` for comments.

```
# .teamcacheignore

# Terraform variable files that may contain secrets
*.tfvars
*.tfvars.json

# Service account key files
*-service-account*.json
*-credentials*.json

# Local developer overrides
config/local.yaml
```

Patterns are matched against relative file paths using standard glob semantics (`*` matches within a single path component; `**` matches across directories). Invalid patterns are silently skipped.

The hardcoded denylist above is enforced independently of `.teamcacheignore` and cannot be removed from it.

---

## Secret scanner

Before any AI-generated summary is written to the cache, TeamCache scans the summary text for credential patterns. If a match is found, the write is **rejected** and a warning is logged. The source file is not read or stored; only the summary text produced by the AI tool is scanned.

The scanner covers the following credential families:

- PEM private keys (RSA, EC, DSA, OpenSSH, encrypted, PGP)
- AWS access key IDs and secret access keys
- GitHub personal access tokens and fine-grained tokens (`ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`)
- OpenAI API keys (`sk-`) and Anthropic API keys (`sk-ant-api`)
- Stripe live and test secret keys
- Slack bot/user/app tokens and incoming webhook URLs
- Google API keys (`AIza`) and OAuth tokens (`ya29.`)
- HuggingFace tokens (`hf_`)
- PyPI upload tokens and npm automation tokens
- SendGrid API keys
- Twilio account SIDs
- Databricks personal access tokens
- Cloudflare API tokens
- Heroku API keys (UUID format)
- Azure Blob Storage connection strings
- Database connection URLs with embedded passwords (PostgreSQL, MySQL, MongoDB, Redis)
- Generic `secret=`, `api_key=`, `auth_token=`, `access_token=`, `private_key=` assignments with 32+ character values
- JSON Web Tokens (JWTs)
- Encrypted private key passphrase markers
- `Authorization: Bearer` headers

The scanner inspects summary text only. It does not read or parse source file content. Static summaries (produced by the tree-sitter parser) contain only symbol names and import paths and carry negligible secret exposure risk; the scanner is applied primarily to AI-generated summaries.

---

## Auditing committed summaries

To inspect all committed summary objects in your repository for potential credential leakage, run:

```bash
grep -r \
  -e "PRIVATE KEY" \
  -e "AKIA" \
  -e "ghp_" \
  -e "sk-" \
  -e "sk-ant-api" \
  -e "AIza" \
  -e "xox[baprs]-" \
  -e "password" \
  -e "secret" \
  -e "token" \
  .teamcache/objects/
```

For a broader scan using git history (including objects that have since been removed):

```bash
git log --all --full-history -- '.teamcache/objects/**' | \
  git diff-tree --no-commit-id -r --stdin | \
  grep -i "secret\|password\|token\|private_key"
```

If you find a committed secret, treat it as compromised. Rotate the credential immediately, then use `git filter-repo` (or a service such as GitHub's secret scanning alert remediation workflow) to purge the object from history and force-push to all remotes.

---

## Reporting vulnerabilities

To report a security vulnerability, open a GitHub issue at:

**https://github.com/siddparab/team-cache/issues**

Please include:

- A description of the vulnerability and its potential impact.
- Reproduction steps or a minimal example.
- The version of TeamCache you are using (`pip show teamcache`).

For sensitive disclosures that should not be public, email the address listed in the git log (`git log --format="%ae" | head -1`) with the subject line `[teamcache] Security disclosure`.

There is no formal SLA, but issues labelled `security` will be prioritised.

---

## Known limitations

**Gitignored files are not indexed, but are not explicitly blocked.**
TeamCache uses `git ls-files` to enumerate files, which means gitignored files are excluded from indexing by default. However, if a file is tracked by git (i.e., was added before being gitignored, or the `.gitignore` entry was added later), it will be included unless it also matches the hardcoded denylist or `.teamcacheignore`. Audit your tracked files with `git ls-files` if you are uncertain.

**Summary accuracy is not guaranteed.**
Summaries are natural-language descriptions produced by an AI tool. They may contain paraphrased content from the original file. In rare cases an AI tool may inadvertently reproduce a credential or sensitive value verbatim in a summary. The secret scanner mitigates this, but pattern-based scanning is not exhaustive. Do not store credentials in source files in repositories where TeamCache is active.

**Git history is permanent without explicit remediation.**
Once a summary object is committed and pushed, it is part of the repository's git history. Removing the file in a subsequent commit does not remove it from history. If a secret is discovered in a committed summary, the full remediation path (rotate, purge history, force-push, notify all clones) must be followed. This is the same consideration that applies to any secret accidentally committed to git.

**The `.teamcacheignore` file itself is not secret.**
`.teamcacheignore` is a plain text file committed to the repository. Listing a path in it does not prevent the underlying file from being committed to git by other means; it only prevents TeamCache from indexing that file. Use `.gitignore` or repository-level access controls for files that must not be committed at all.
