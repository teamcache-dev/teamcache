import subprocess
import pytest
import os
from pathlib import Path


@pytest.fixture
def tmp_git_repo(tmp_path):
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def run_teamcache_init(root, flags=None):
    cmd = ["python", "-m", "teamcache", "init"] + (flags or [])
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True)


def run_teamcache_index(root):
    cmd = ["python", "-m", "teamcache", "index"]
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True)
