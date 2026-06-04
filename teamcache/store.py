from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from .constants import SUMMARIES_DIR, SYMBOLS_DIR


@runtime_checkable
class ObjectStore(Protocol):
    def read(self, key: str) -> dict | None: ...
    def write(self, key: str, obj: dict) -> None: ...
    def list(self, prefix: str = "") -> list[str]: ...


class GitObjectStore:
    """Reads/writes .teamcache/objects/ on disk using content-addressed JSON files."""

    def __init__(self, summaries_root: Path, symbols_root: Path) -> None:
        self._summaries_root = summaries_root
        self._symbols_root = symbols_root

    def _summary_path(self, key: str) -> Path:
        return self._summaries_root / key[:2] / f"{key[:12]}_v1.json"

    def _symbol_path(self, key: str) -> Path:
        return self._symbols_root / key[:2] / f"{key[:12]}_v1.json"

    def read(self, key: str) -> dict | None:
        """Read a summary object by cache key. Returns None if not found or malformed."""
        path = self._summary_path(key)
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(obj, dict):
            return None
        required = {"cache_key", "file_path", "file_hash", "summary", "summary_type"}
        return obj if required.issubset(obj) else None

    def read_symbol(self, key: str) -> dict | None:
        """Read a symbol object by cache key. Returns None if not found or malformed."""
        path = self._symbol_path(key)
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(obj, dict):
            return None
        required = {"cache_key", "file_path", "file_hash", "symbols"}
        return obj if required.issubset(obj) and isinstance(obj.get("symbols"), dict) else None

    def write(self, key: str, obj: dict) -> None:
        """Write a summary object atomically to the summaries directory."""
        if "summary_type" not in obj:
            raise ValueError("summary_type is required")
        self._atomic_write(self._summary_path(key), obj)

    def write_symbol(self, key: str, obj: dict) -> None:
        """Write a symbol object atomically to the symbols directory."""
        self._atomic_write(self._symbol_path(key), obj)

    def list(self, prefix: str = "") -> list[str]:
        """Return relative paths of all JSON files under summaries_root matching prefix."""
        root = self._summaries_root
        if not root.exists():
            return []
        results: list[str] = []
        for path in root.glob("**/*.json"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(prefix):
                results.append(rel)
        return sorted(results)

    @staticmethod
    def _atomic_write(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def make_store(config: object, repo_root: Path) -> GitObjectStore:
    """Return the configured ObjectStore implementation.

    Currently only the 'git' backend (local filesystem) is supported.
    The objects_backend config field is reserved for future backends.
    """
    backend = getattr(config, "objects_backend", "git")
    if backend == "git":
        return GitObjectStore(
            repo_root / SUMMARIES_DIR,
            repo_root / SYMBOLS_DIR,
        )
    raise ValueError(f"Unknown backend: {backend!r}. Currently only 'git' is supported.")
