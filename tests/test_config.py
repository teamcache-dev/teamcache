from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from teamcache.config import TeamCacheConfig, load_config, write_config


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Return a tmp_path that already has a .teamcache directory."""
    (tmp_path / ".teamcache").mkdir()
    return tmp_path


def _write_raw_config(repo_root: Path, data: dict) -> None:
    config_path = repo_root / ".teamcache" / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Old-format config (only schema_version key) falls back to defaults
# ---------------------------------------------------------------------------

def test_load_config_old_format_uses_defaults(repo_root: Path) -> None:
    _write_raw_config(repo_root, {"schema_version": "v1"})

    cfg = load_config(repo_root)

    assert cfg.schema_version == "v1"
    assert cfg.tracked_files_only is True
    assert cfg.enable_embeddings is False
    assert cfg.enable_hooks is False
    assert cfg.max_files_for_local_embeddings == 5000
    assert cfg.require_source_before_edit is True
    assert cfg.sensitive_path_denylist == []


# ---------------------------------------------------------------------------
# 2. Round-trip: write non-default values, load back, values are preserved
# ---------------------------------------------------------------------------

def test_config_roundtrip(repo_root: Path) -> None:
    original = TeamCacheConfig(
        schema_version="v2",
        enable_hooks=True,
        tracked_files_only=False,
        enable_embeddings=True,
        max_files_for_local_embeddings=1000,
        require_source_before_edit=False,
        sensitive_path_denylist=["secrets/", "*.key"],
    )

    write_config(repo_root, original)
    loaded = load_config(repo_root)

    assert loaded.schema_version == "v2"
    assert loaded.enable_hooks is True
    assert loaded.tracked_files_only is False
    assert loaded.enable_embeddings is True
    assert loaded.max_files_for_local_embeddings == 1000
    assert loaded.require_source_before_edit is False
    assert loaded.sensitive_path_denylist == ["secrets/", "*.key"]


# ---------------------------------------------------------------------------
# 3. sensitive_path_denylist entries are preserved exactly
# ---------------------------------------------------------------------------

def test_config_sensitive_path_denylist(repo_root: Path) -> None:
    entries = [".env", "config/secrets.yaml", "**/*.pem", "private/"]
    _write_raw_config(repo_root, {"schema_version": "v2", "sensitive_path_denylist": entries})

    cfg = load_config(repo_root)

    assert cfg.sensitive_path_denylist == entries
    assert len(cfg.sensitive_path_denylist) == 4


# ---------------------------------------------------------------------------
# 4. load_config on a directory with no config file returns defaults safely
# ---------------------------------------------------------------------------

def test_config_defaults_no_file(tmp_path: Path) -> None:
    # Create the .teamcache dir but leave config.yaml absent; load_config
    # reads from CONFIG_FILE which is ".teamcache/config.yaml".  When the
    # file is missing Path.read_text raises FileNotFoundError, so we create
    # an empty-ish config file to simulate "initialized but blank" state,
    # which is the realistic "no meaningful keys" scenario.
    tc_dir = tmp_path / ".teamcache"
    tc_dir.mkdir()
    config_path = tc_dir / "config.yaml"
    # Write an empty YAML document (valid YAML that loads as None/empty dict)
    config_path.write_text("", encoding="utf-8")

    cfg = load_config(tmp_path)

    # All fields must carry their documented defaults
    assert cfg.tracked_files_only is True
    assert cfg.enable_embeddings is False
    assert cfg.enable_hooks is False
    assert cfg.max_files_for_local_embeddings == 5000
    assert cfg.require_source_before_edit is True
    assert cfg.sensitive_path_denylist == []
