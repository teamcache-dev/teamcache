from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .constants import CONFIG_FILE, SCHEMA_VERSION


@dataclass
class TeamCacheConfig:
    schema_version: str = SCHEMA_VERSION
    enable_hooks: bool = False
    tracked_files_only: bool = True
    enable_embeddings: bool = False
    max_files_for_local_embeddings: int = 5000
    require_source_before_edit: bool = True
    sensitive_path_denylist: list = field(default_factory=list)
    objects_backend: str = "git"
    objects_backend_url: str = ""
    scope_paths: list[str] = field(default_factory=list)
    # If non-empty, only files under these paths are indexed/served


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILE).exists():
            return candidate
    raise RuntimeError("TeamCache config not found. Run: teamcache init")


def load_config(repo_root: Path) -> TeamCacheConfig:
    config_path = repo_root / CONFIG_FILE
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return TeamCacheConfig(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        enable_hooks=bool(data.get("enable_hooks", False)),
        tracked_files_only=bool(data.get("tracked_files_only", True)),
        enable_embeddings=bool(data.get("enable_embeddings", False)),
        max_files_for_local_embeddings=int(data.get("max_files_for_local_embeddings", 5000)),
        require_source_before_edit=bool(data.get("require_source_before_edit", True)),
        sensitive_path_denylist=list(data.get("sensitive_path_denylist", [])),
        objects_backend=str(data.get("objects_backend", "git")),
        objects_backend_url=str(data.get("objects_backend_url", "")),
        scope_paths=list(data.get("scope_paths", [])),
    )


def write_config(repo_root: Path, config: TeamCacheConfig) -> None:
    config_path = repo_root / CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(
        {
            "schema_version": config.schema_version,
            "enable_hooks": config.enable_hooks,
            "tracked_files_only": config.tracked_files_only,
            "enable_embeddings": config.enable_embeddings,
            "max_files_for_local_embeddings": config.max_files_for_local_embeddings,
            "require_source_before_edit": config.require_source_before_edit,
            "sensitive_path_denylist": config.sensitive_path_denylist,
            "objects_backend": config.objects_backend,
            "objects_backend_url": config.objects_backend_url,
            "scope_paths": config.scope_paths,
        },
        sort_keys=False,
    )
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(config_path)
