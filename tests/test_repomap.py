from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from teamcache.cli import cli


@pytest.fixture()
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    return tmp_path


def _init_and_index(repo: Path, runner: CliRunner) -> None:
    import os
    os.chdir(repo)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    assert runner.invoke(cli, ["index"]).exit_code == 0


def _add_tracked(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, check=True)


def _repomap(repo: Path) -> dict:
    return json.loads(
        (repo / ".teamcache" / "objects" / "repomap.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 1. reverse_imports key is present in repomap output
# ---------------------------------------------------------------------------

def test_repomap_has_reverse_imports_key(repo):
    runner = CliRunner()
    _init_and_index(repo, runner)
    rm = _repomap(repo)
    assert "reverse_imports" in rm


# ---------------------------------------------------------------------------
# 2. Single importer — utils.py imported by main.py
# ---------------------------------------------------------------------------

def test_reverse_imports_single_importer(repo):
    _add_tracked(repo, "utils.py", "def helper(): pass\n")
    _add_tracked(repo, "main.py", "from utils import helper\n\ndef run(): helper()\n")

    runner = CliRunner()
    _init_and_index(repo, runner)

    rm = _repomap(repo)
    ri = rm["reverse_imports"]

    # utils.py should appear as a key because main.py imports it
    assert "utils.py" in ri
    assert "main.py" in ri["utils.py"]


# ---------------------------------------------------------------------------
# 3. Multiple importers of the same file
# ---------------------------------------------------------------------------

def test_reverse_imports_multiple_importers(repo):
    _add_tracked(repo, "shared.py", "X = 1\n")
    _add_tracked(repo, "a.py", "from shared import X  # module a\n")
    _add_tracked(repo, "b.py", "from shared import X  # module b\n")

    runner = CliRunner()
    _init_and_index(repo, runner)

    ri = _repomap(repo)["reverse_imports"]
    assert "shared.py" in ri
    importers = ri["shared.py"]
    assert "a.py" in importers
    assert "b.py" in importers


# ---------------------------------------------------------------------------
# 4. A file that imports nothing should not appear as an importer
# ---------------------------------------------------------------------------

def test_reverse_imports_no_self_loops(repo):
    _add_tracked(repo, "standalone.py", "X = 42\n")

    runner = CliRunner()
    _init_and_index(repo, runner)

    ri = _repomap(repo)["reverse_imports"]
    # standalone.py has no imports so it should never appear as an importer
    for importers in ri.values():
        assert "standalone.py" not in importers


# ---------------------------------------------------------------------------
# 5. Entries are deduplicated — indexing twice does not double entries
# ---------------------------------------------------------------------------

def test_reverse_imports_no_duplicates(repo):
    _add_tracked(repo, "lib.py", "def fn(): pass\n")
    _add_tracked(repo, "consumer.py", "from lib import fn\n")

    runner = CliRunner()
    import os
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])
    runner.invoke(cli, ["index"])  # second run

    ri = _repomap(repo)["reverse_imports"]
    if "lib.py" in ri:
        assert ri["lib.py"].count("consumer.py") == 1
