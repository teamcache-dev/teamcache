from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .constants import CONFIG_FILE, SCHEMA_VERSION


@dataclass
class TeamCacheConfig:
    schema_version: str = SCHEMA_VERSION


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILE).exists():
            return candidate
    raise RuntimeError("TeamCache config not found. Run: teamcache init")


def load_config(repo_root: Path) -> TeamCacheConfig:
    config_path = repo_root / CONFIG_FILE
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return TeamCacheConfig(schema_version=data.get("schema_version", SCHEMA_VERSION))


def write_config(repo_root: Path, config: TeamCacheConfig) -> None:
    config_path = repo_root / CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(
        {"schema_version": config.schema_version},
        sort_keys=False,
    )
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(config_path)
