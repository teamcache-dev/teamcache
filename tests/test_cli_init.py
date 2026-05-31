from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from teamcache.cli import cli


@pytest.fixture()
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)
    return tmp_path


def test_init_creates_structure(repo):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=repo):
        import os
        os.chdir(repo)
        result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0
    assert (repo / ".teamcache" / "objects" / "summaries").is_dir()
    assert (repo / ".teamcache" / "objects" / "symbols").is_dir()
    assert (repo / ".teamcache" / "local").is_dir()
    assert (repo / ".teamcache" / "config.yaml").is_file()


def test_init_adds_gitignore_entry(repo):
    runner = CliRunner()
    import os
    os.chdir(repo)
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0
    gitignore = repo / ".gitignore"
    assert gitignore.exists()
    assert ".teamcache/local/" in gitignore.read_text()


def test_init_idempotent(repo):
    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0
    # .gitignore entry not duplicated
    content = (repo / ".gitignore").read_text()
    assert content.count(".teamcache/local/") == 1


def test_index_on_empty_repo(repo):
    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    result = runner.invoke(cli, ["index"])
    assert result.exit_code == 0
    assert "Indexed:" in result.output


def test_index_creates_summary_objects(repo):
    # Write a Python file and index it
    (repo / "hello.py").write_text("def greet(): pass\n")
    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    result = runner.invoke(cli, ["index"])
    assert result.exit_code == 0
    summaries = list((repo / ".teamcache" / "objects" / "summaries").glob("**/*.json"))
    assert len(summaries) >= 1


def test_stats_runs(repo):
    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])
    result = runner.invoke(cli, ["stats"])
    assert result.exit_code == 0
    assert "TeamCache Stats" in result.output


def test_report_creates_file(repo):
    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])
    result = runner.invoke(cli, ["report"])
    assert result.exit_code == 0
    reports = list((repo / ".teamcache" / "reports").glob("*.md"))
    assert len(reports) == 1
