from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterator

from .constants import (
    BINARY_EXTENSIONS,
    MAX_FILE_BYTES,
    SKIP_DIRS,
    SKIP_SUFFIXES,
)


# ---------------------------------------------------------------------------
# Sensitive path denylist — hardcoded, cannot be overridden by configuration.
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS = [
    re.compile(r"(^|[/\\])\.env(\.[^/\\]+)?$", re.IGNORECASE),
    re.compile(r"(^|[/\\])secrets?[/\\]", re.IGNORECASE),
    re.compile(r"(^|[/\\])credentials?[/\\]", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.aws[/\\]", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.ssh[/\\]", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.gnupg[/\\]", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx|jks|keystore|ppk)$", re.IGNORECASE),
    re.compile(r"(^|[/\\])id_(rsa|ecdsa|ed25519|dsa)$"),
    re.compile(r"(^|[/\\])\.netrc$", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.npmrc$", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.pypirc$", re.IGNORECASE),
]


def is_sensitive_path(rel_path: str) -> bool:
    normalised = rel_path.replace("\\", "/")
    return any(p.search(normalised) for p in _SENSITIVE_PATH_PATTERNS)


# ---------------------------------------------------------------------------
# .teamcacheignore support
# ---------------------------------------------------------------------------

def load_teamcacheignore(root: Path) -> list:
    path = root / ".teamcacheignore"
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(fnmatch.translate(line)))
        except re.error:
            pass
    return patterns


# ---------------------------------------------------------------------------
# File iteration — always uses git ls-files for safety
# ---------------------------------------------------------------------------

def iter_repo_files(root: Path, tracked_only: bool = True, extra_ignores: list | None = None) -> Iterator[Path]:
    extra_ignores = extra_ignores or []
    if tracked_only:
        yield from _iter_tracked(root, extra_ignores)
    else:
        yield from _iter_tracked(root, extra_ignores)  # fallback: still use git ls-files for safety


def _iter_tracked(root: Path, extra_ignores: list) -> Iterator[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--full-name"],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return
    root_resolved = root.resolve()
    for raw in result.stdout.split(b"\x00"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        if is_sensitive_path(rel):
            continue
        if any(p.search(rel) for p in extra_ignores):
            continue
        path = (root / rel).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError:
            continue
        if path.is_file():
            yield path


def is_binary_content(path: Path, content: bytes) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    return b"\x00" in content[:4096]


def should_skip_metadata(path: Path) -> bool:
    try:
        if path.suffix.lower() in SKIP_SUFFIXES:
            return True
        if path.stat().st_size > MAX_FILE_BYTES:
            return True
        return False
    except OSError:
        return True


def static_summary(path: Path, language: str, text: str | None = None) -> str:
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "binary or unknown structure"

    parts: list[str] = []

    if language == "python":
        imports = _unique(
            re.findall(r"^import\s+(\S+)", text, re.MULTILINE)
            + re.findall(r"^from\s+(\S+)\s+import", text, re.MULTILINE)
        )
        classes = _unique(re.findall(r"^class\s+(\w+)", text, re.MULTILINE))
        functions = _unique(re.findall(r"^def\s+(\w+)", text, re.MULTILINE))
        _append(parts, "imports", imports)
        _append(parts, "classes", classes)
        _append(parts, "functions", functions)
    elif language in {"javascript", "typescript"}:
        imports = _unique(
            re.findall(r"^\s*import\s+.*?\s+from\s+[\"']([^\"']+)[\"']", text, re.MULTILINE)
            + re.findall(r"require\([\"']([^\"']+)[\"']\)", text)
        )
        exports = _unique(
            re.findall(
                r"^\s*export\s+(?:default\s+)?(?:class|function|const)\s+(\w+)",
                text,
                re.MULTILINE,
            )
        )
        classes = _unique(re.findall(r"^\s*class\s+(\w+)", text, re.MULTILINE))
        functions = _unique(
            re.findall(r"^\s*function\s+(\w+)", text, re.MULTILINE)
            + re.findall(
                r"^\s*const\s+(\w+)\s*=\s*(?:async\s*)?(?:(?:\w+|\(.*?\))\s*=>|\()",
                text,
                re.MULTILINE,
            )
        )
        _append(parts, "imports", imports)
        _append(parts, "exports", exports)
        _append(parts, "classes", classes)
        _append(parts, "functions", functions)
    elif language == "go":
        packages = _unique(re.findall(r"^package\s+(\w+)", text, re.MULTILINE))
        imports = _go_imports(text)
        functions = _unique(
            re.findall(r"^func\s+(?:\([^)]+\)\s*)?(\w+)", text, re.MULTILINE)
        )
        types = _unique(re.findall(r"^type\s+(\w+)\s+struct", text, re.MULTILINE))
        _append(parts, "package", packages)
        _append(parts, "imports", imports)
        _append(parts, "functions", functions)
        _append(parts, "types", types)
    else:
        symbols = _unique(
            re.findall(r"^\s*(?:def|func|function)\s+(\w+)", text, re.MULTILINE)
            + re.findall(r"^\s*class\s+(\w+)", text, re.MULTILINE)
            + re.findall(r"^\s*type\s+(\w+)", text, re.MULTILINE)
        )
        _append(parts, "symbols", symbols)
        if not parts:
            return "binary or unknown structure"

    return " | ".join(parts) if parts else "no extractable structure"


def cache_key(file_hash: str, schema_version: str) -> str:
    return hashlib.sha256(f"{file_hash}|{schema_version}".encode()).hexdigest()


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_summary_object(summaries_root: Path, key: str, obj: dict[str, object]) -> None:
    if "summary_type" not in obj:
        raise ValueError("summary_type is required")
    prefix_dir = summaries_root / key[:2]
    prefix_dir.mkdir(parents=True, exist_ok=True)
    final = prefix_dir / f"{key[:12]}_v1.json"
    tmp = final.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)


def git_user_email(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    email = result.stdout.strip()
    return email if result.returncode == 0 and email else "unknown"


def _append(parts: list[str], label: str, values: list[str]) -> None:
    if values:
        parts.append(f"{label}: {', '.join(values)}")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _go_imports(text: str) -> list[str]:
    imports: list[str] = []
    for block in re.findall(r"import\s*\((.*?)\)", text, re.DOTALL):
        imports.extend(re.findall(r'"([^"]+)"', block))
    imports.extend(re.findall(r'^import\s+"([^"]+)"', text, re.MULTILINE))
    return _unique(imports)
