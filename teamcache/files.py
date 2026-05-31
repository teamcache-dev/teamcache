from __future__ import annotations

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


def iter_repo_files(root: Path) -> Iterator[Path]:
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [dirname for dirname in dirs if dirname not in SKIP_DIRS]
        for filename in files:
            yield Path(dirpath) / filename


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
