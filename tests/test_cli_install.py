from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import teamcache.cli as cli_module
from teamcache.cli import cli


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    monkeypatch.setattr(cli_module, "_resolve_teamcache_bin", lambda: Path("teamcache"))
    return tmp_path


def invoke(args):
    return CliRunner().invoke(cli, args)


def test_install_preserves_existing_agents_content(repo):
    original = "# Existing agent notes\nDo not remove this.\n"
    (repo / "AGENTS.md").write_text(original, encoding="utf-8")

    result = invoke(["install", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    content = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert content.startswith(original)
    assert content.count("<!-- TEAMCACHE:START -->") == 1
    assert content.count("<!-- TEAMCACHE:END -->") == 1
    assert "<!-- teamcache-installed -->" not in content


def test_install_preserves_existing_claude_content(repo, monkeypatch):
    original = "# Claude project notes\nKeep this line.\n"
    (repo / "CLAUDE.md").write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    result = invoke(["install", "--agent", "claude"])

    assert result.exit_code == 0, result.output
    content = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert content.startswith(original)
    assert content.count("<!-- TEAMCACHE:START -->") == 1
    assert content.count("<!-- TEAMCACHE:END -->") == 1


def test_install_preserves_existing_cursorrules_content(repo):
    original = "Always prefer the local style.\n"
    (repo / ".cursorrules").write_text(original, encoding="utf-8")

    result = invoke(["install", "--agent", "cursor"])

    assert result.exit_code == 0, result.output
    content = (repo / ".cursorrules").read_text(encoding="utf-8")
    assert content.startswith(original)
    assert content.count("<!-- TEAMCACHE:START -->") == 1
    assert content.count("<!-- TEAMCACHE:END -->") == 1


def test_install_preserves_existing_windsurfrules_content(repo):
    original = "Use project-specific Windsurf rules.\n"
    (repo / ".windsurfrules").write_text(original, encoding="utf-8")

    result = invoke(["install", "--agent", "windsurf"])

    assert result.exit_code == 0, result.output
    content = (repo / ".windsurfrules").read_text(encoding="utf-8")
    assert content.startswith(original)
    assert content.count("<!-- TEAMCACHE:START -->") == 1
    assert content.count("<!-- TEAMCACHE:END -->") == 1


def test_repeated_install_is_idempotent(repo):
    first = invoke(["install", "--agent", "codex"])
    second = invoke(["install", "--agent", "codex"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    content = (repo / "AGENTS.md").read_text(encoding="utf-8")
    config = json.loads((repo / ".codex" / "config.json").read_text(encoding="utf-8"))
    assert content.count("<!-- TEAMCACHE:START -->") == 1
    assert list(config["mcpServers"]).count("teamcache") == 1


def test_repeated_uninstall_is_idempotent(repo):
    assert invoke(["install", "--agent", "codex"]).exit_code == 0

    first = invoke(["uninstall", "--agent", "codex"])
    second = invoke(["uninstall", "--agent", "codex"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "<!-- TEAMCACHE:START -->" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    config = json.loads((repo / ".codex" / "config.json").read_text(encoding="utf-8"))
    assert "teamcache" not in config["mcpServers"]


def test_uninstall_without_teamcache_block_is_noop(repo):
    original = "User-owned instructions only.\n"
    (repo / "AGENTS.md").write_text(original, encoding="utf-8")
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}, indent=2) + "\n",
        encoding="utf-8",
    )

    result = invoke(["uninstall", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == original
    config = json.loads((repo / ".codex" / "config.json").read_text(encoding="utf-8"))
    assert config == {"mcpServers": {"other": {"command": "other"}}}
    assert not (repo / "AGENTS.md.teamcache.bak").exists()


def test_install_uninstall_round_trip_preserves_files_without_trailing_newline(repo, monkeypatch):
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    cases = [
        ("codex", "AGENTS.md"),
        ("claude", "CLAUDE.md"),
        ("cursor", ".cursorrules"),
        ("windsurf", ".windsurfrules"),
    ]

    for agent, filename in cases:
        path = repo / filename
        original = f"{filename} user content without trailing newline"
        path.write_text(original, encoding="utf-8")
        assert invoke(["install", "--agent", agent]).exit_code == 0
        assert invoke(["uninstall", "--agent", agent]).exit_code == 0
        assert path.read_text(encoding="utf-8") == original


def test_malformed_cursor_mcp_json_fails_closed(repo):
    (repo / ".cursor").mkdir()
    mcp_path = repo / ".cursor" / "mcp.json"
    mcp_path.write_text("{not json", encoding="utf-8")
    original_rules = "Cursor rules stay untouched.\n"
    (repo / ".cursorrules").write_text(original_rules, encoding="utf-8")

    result = invoke(["install", "--agent", "cursor"])

    assert result.exit_code != 0
    assert "malformed JSON" in result.output
    assert mcp_path.read_text(encoding="utf-8") == "{not json"
    assert (repo / ".cursorrules").read_text(encoding="utf-8") == original_rules


def test_malformed_cursor_mcp_json_fails_closed_on_uninstall(repo):
    (repo / ".cursor").mkdir()
    mcp_path = repo / ".cursor" / "mcp.json"
    mcp_path.write_text("{not json", encoding="utf-8")
    rules = "<!-- TEAMCACHE:START -->\n# TeamCache\n<!-- TEAMCACHE:END -->\n"
    (repo / ".cursorrules").write_text(rules, encoding="utf-8")

    result = invoke(["uninstall", "--agent", "cursor"])

    assert result.exit_code != 0
    assert "malformed JSON" in result.output
    assert mcp_path.read_text(encoding="utf-8") == "{not json"
    assert (repo / ".cursorrules").read_text(encoding="utf-8") == rules


def test_json_config_with_invalid_shape_fails_closed(repo):
    (repo / ".cursor").mkdir()
    mcp_path = repo / ".cursor" / "mcp.json"
    mcp_path.write_text("[]\n", encoding="utf-8")
    original_rules = "Cursor rules stay untouched.\n"
    (repo / ".cursorrules").write_text(original_rules, encoding="utf-8")

    result = invoke(["install", "--agent", "cursor"])

    assert result.exit_code != 0
    assert "must be an object" in result.output
    assert mcp_path.read_text(encoding="utf-8") == "[]\n"
    assert (repo / ".cursorrules").read_text(encoding="utf-8") == original_rules


def test_json_config_with_invalid_mcpservers_shape_fails_closed_on_uninstall(repo):
    (repo / ".cursor").mkdir()
    mcp_path = repo / ".cursor" / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": "bad"}) + "\n", encoding="utf-8")
    rules = "<!-- TEAMCACHE:START -->\n# TeamCache\n<!-- TEAMCACHE:END -->\n"
    (repo / ".cursorrules").write_text(rules, encoding="utf-8")

    result = invoke(["uninstall", "--agent", "cursor"])

    assert result.exit_code != 0
    assert "mcpServers" in result.output
    assert mcp_path.read_text(encoding="utf-8") == json.dumps({"mcpServers": "bad"}) + "\n"
    assert (repo / ".cursorrules").read_text(encoding="utf-8") == rules


def test_dry_run_creates_or_modifies_no_files(repo):
    result = invoke(["install", "--agent", "codex", "--dry-run", "--print-diff"])

    assert result.exit_code == 0, result.output
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / ".codex").exists()
    assert "<!-- TEAMCACHE:START -->" in result.output
    assert "+  \"mcpServers\": {" in result.output


def test_print_diff_shows_expected_changes(repo):
    result = invoke(["install", "--agent", "codex", "--dry-run", "--print-diff"])

    assert result.exit_code == 0, result.output
    assert "--- " in result.output
    assert "+++ " in result.output
    assert "+<!-- TEAMCACHE:START -->" in result.output
    assert "+    \"teamcache\": {" in result.output


def test_uninstall_dry_run_creates_or_modifies_no_files(repo):
    assert invoke(["install", "--agent", "codex"]).exit_code == 0
    agents_before = (repo / "AGENTS.md").read_text(encoding="utf-8")
    config_before = (repo / ".codex" / "config.json").read_text(encoding="utf-8")

    result = invoke(["uninstall", "--agent", "codex", "--dry-run", "--print-diff"])

    assert result.exit_code == 0, result.output
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == agents_before
    assert (repo / ".codex" / "config.json").read_text(encoding="utf-8") == config_before
    assert "-<!-- TEAMCACHE:START -->" in result.output
    assert "-    \"teamcache\": {" in result.output


def test_backup_files_are_created_before_install_mutation(repo):
    agents_original = "Existing AGENTS content.\n"
    config_original = {"mcpServers": {"other": {"command": "other"}}}
    (repo / "AGENTS.md").write_text(agents_original, encoding="utf-8")
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.json").write_text(
        json.dumps(config_original, indent=2) + "\n",
        encoding="utf-8",
    )

    result = invoke(["install", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    assert (repo / "AGENTS.md.teamcache.bak").read_text(encoding="utf-8") == agents_original
    backup_config = json.loads((repo / ".codex" / "config.json.teamcache.bak").read_text(encoding="utf-8"))
    assert backup_config == config_original


def test_backup_file_is_created_before_uninstall_mutation(repo):
    assert invoke(["install", "--agent", "codex"]).exit_code == 0
    installed = (repo / "AGENTS.md").read_text(encoding="utf-8")

    result = invoke(["uninstall", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    assert (repo / "AGENTS.md.teamcache.bak").read_text(encoding="utf-8") == installed


def test_install_gemini_writes_extension_files(repo, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cli_module, "_gemini_extension_dir", lambda: fake_home / ".gemini" / "extensions" / "teamcache")

    result = invoke(["install", "--agent", "gemini"])

    assert result.exit_code == 0, result.output
    ext_dir = fake_home / ".gemini" / "extensions" / "teamcache"
    manifest = json.loads((ext_dir / "gemini-extension.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "teamcache"
    assert "teamcache" in manifest["mcpServers"]
    assert manifest["mcpServers"]["teamcache"]["args"] == ["serve"]
    assert manifest["contextFileName"] == "GEMINI.md"
    context = (ext_dir / "GEMINI.md").read_text(encoding="utf-8")
    assert "repo_overview" in context
    assert "cache_summary" in context


def test_uninstall_gemini_removes_extension_files(repo, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cli_module, "_gemini_extension_dir", lambda: fake_home / ".gemini" / "extensions" / "teamcache")

    assert invoke(["install", "--agent", "gemini"]).exit_code == 0
    ext_dir = fake_home / ".gemini" / "extensions" / "teamcache"
    assert (ext_dir / "gemini-extension.json").exists()
    assert (ext_dir / "GEMINI.md").exists()

    result = invoke(["uninstall", "--agent", "gemini"])

    assert result.exit_code == 0, result.output
    assert not (ext_dir / "gemini-extension.json").exists()
    assert not (ext_dir / "GEMINI.md").exists()


def test_install_gemini_is_idempotent(repo, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cli_module, "_gemini_extension_dir", lambda: fake_home / ".gemini" / "extensions" / "teamcache")

    assert invoke(["install", "--agent", "gemini"]).exit_code == 0
    first = (fake_home / ".gemini" / "extensions" / "teamcache" / "gemini-extension.json").read_text(encoding="utf-8")
    assert invoke(["install", "--agent", "gemini"]).exit_code == 0
    second = (fake_home / ".gemini" / "extensions" / "teamcache" / "gemini-extension.json").read_text(encoding="utf-8")
    assert first == second
