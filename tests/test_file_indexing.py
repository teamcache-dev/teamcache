from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from teamcache.files import iter_repo_files, is_sensitive_path, load_teamcacheignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _rel_names(root: Path, paths) -> set[str]:
    """Convert absolute Path objects yielded by iter_repo_files into relative POSIX strings."""
    result = set()
    for p in paths:
        try:
            result.add(p.relative_to(root).as_posix())
        except ValueError:
            result.add(p.as_posix())
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_git_repo(tmp_path):
    """Minimal git repo with user config so commits work."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# is_sensitive_path — unit tests (no filesystem needed)
# ---------------------------------------------------------------------------

def test_is_sensitive_path_env():
    assert is_sensitive_path(".env") is True


def test_is_sensitive_path_env_local():
    assert is_sensitive_path(".env.local") is True


def test_is_sensitive_path_env_production():
    assert is_sensitive_path(".env.production") is True


def test_is_sensitive_path_src_env():
    assert is_sensitive_path("src/.env.staging") is True


def test_is_sensitive_path_pem():
    assert is_sensitive_path("certs/server.pem") is True


def test_is_sensitive_path_key():
    assert is_sensitive_path("private.key") is True


def test_is_sensitive_path_id_rsa():
    assert is_sensitive_path("id_rsa") is True


def test_is_sensitive_path_id_ed25519():
    assert is_sensitive_path("id_ed25519") is True


def test_is_sensitive_path_ssh_dir():
    assert is_sensitive_path(".ssh/config") is True


def test_is_sensitive_path_netrc():
    assert is_sensitive_path(".netrc") is True


def test_is_sensitive_path_npmrc():
    assert is_sensitive_path(".npmrc") is True


def test_is_sensitive_path_normal_py():
    assert is_sensitive_path("src/main.py") is False


def test_is_sensitive_path_config_py():
    assert is_sensitive_path("src/config.py") is False


# ---------------------------------------------------------------------------
# iter_repo_files on non-git directory
# ---------------------------------------------------------------------------

def test_no_git_repo_returns_empty_iterator(tmp_path):
    """iter_repo_files on a plain directory (no .git) must yield nothing."""
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    results = list(iter_repo_files(tmp_path))
    assert results == []


# ---------------------------------------------------------------------------
# load_teamcacheignore — unit test
# ---------------------------------------------------------------------------

def test_teamcacheignore_patterns(tmp_path):
    ignore_file = tmp_path / ".teamcacheignore"
    ignore_file.write_text(
        "# comment\n"
        "\n"
        "*.log\n"
        "build/\n",
        encoding="utf-8",
    )
    patterns = load_teamcacheignore(tmp_path)
    # Two non-blank, non-comment lines -> two compiled patterns
    assert len(patterns) == 2
    # The *.log pattern should match 'debug.log'
    assert any(p.search("debug.log") for p in patterns)
    # The build/ pattern should match a path containing 'build/'
    assert any(p.search("build/") for p in patterns)
    # A normal .py file should not match
    assert not any(p.search("main.py") for p in patterns)


# ---------------------------------------------------------------------------
# Integration tests — require tmp_git_repo fixture
# ---------------------------------------------------------------------------

def test_dotenv_gitignored_not_indexed(tmp_git_repo):
    """A .env that is listed in .gitignore must not appear in iter_repo_files results."""
    repo = tmp_git_repo
    env_file = repo / ".env"
    env_file.write_text("SECRET=hunter2\n", encoding="utf-8")

    gitignore = repo / ".gitignore"
    gitignore.write_text(".env\n", encoding="utf-8")

    # Stage and commit the .gitignore (but NOT .env — it is ignored)
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-m", "add gitignore", cwd=repo)

    names = _rel_names(repo, iter_repo_files(repo))
    assert ".env" not in names


def test_dotenv_tracked_blocked_by_denylist(tmp_git_repo):
    """Even if .env is staged/tracked by git, the denylist must block it."""
    repo = tmp_git_repo
    env_file = repo / ".env"
    env_file.write_text("SECRET=hunter2\n", encoding="utf-8")

    # Forcibly add .env to git index despite any ignore rules
    _git("add", "-f", ".env", cwd=repo)
    _git("commit", "-m", "accidentally track .env", cwd=repo)

    names = _rel_names(repo, iter_repo_files(repo))
    assert ".env" not in names


def test_untracked_file_not_indexed(tmp_git_repo):
    """A file that has never been git-added must not appear in results."""
    repo = tmp_git_repo
    # Create a tracked file so the repo has at least one commit
    (repo / "main.py").write_text("pass\n", encoding="utf-8")
    _git("add", "main.py", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    # Create an untracked file
    (repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")

    names = _rel_names(repo, iter_repo_files(repo))
    assert "untracked.py" not in names
    # The tracked file IS present
    assert "main.py" in names


def test_teamcacheignore_excludes_file(tmp_git_repo):
    """A file matching a .teamcacheignore pattern must be excluded from results."""
    repo = tmp_git_repo
    (repo / "app.py").write_text("pass\n", encoding="utf-8")
    (repo / "debug.log").write_text("log output\n", encoding="utf-8")
    ignore_file = repo / ".teamcacheignore"
    ignore_file.write_text("*.log\n", encoding="utf-8")

    # Use -f so git doesn't reject debug.log if a global gitignore blocks *.log
    _git("add", "-f", "app.py", "debug.log", ".teamcacheignore", cwd=repo)
    _git("commit", "-m", "add files", cwd=repo)

    extra_ignores = load_teamcacheignore(repo)
    names = _rel_names(repo, iter_repo_files(repo, extra_ignores=extra_ignores))

    assert "debug.log" not in names
    assert "app.py" in names


def test_filename_with_spaces_is_indexed(tmp_git_repo):
    """Files whose names contain spaces must be decoded and indexed correctly."""
    repo = tmp_git_repo
    spaced = repo / "my component.py"
    spaced.write_text("class MyComponent: pass\n", encoding="utf-8")

    _git("add", "my component.py", cwd=repo)
    _git("commit", "-m", "add component with spaces", cwd=repo)

    names = _rel_names(repo, iter_repo_files(repo))
    assert "my component.py" in names
