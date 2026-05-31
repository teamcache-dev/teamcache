"""
Tests for P0 hook fixes in teamcache.

Covers:
- init does NOT create .githooks/post-commit (only post-merge)
- plain init does NOT set git config core.hooksPath
- --enable-hooks with no conflicts installs .githooks/post-merge
- --enable-hooks with a conflicting executable hook in .git/hooks/ exits non-zero
- --enable-hooks --force-hooks succeeds even with conflicts
- post-merge hook content contains no "amend" line
- migrate-hooks strips amend lines from an existing .githooks/post-commit
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import run_teamcache_init


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_config_get(root: Path, key: str) -> str | None:
    """Return the value of a git config key, or None if not set."""
    result = subprocess.run(
        ["git", "config", "--local", key],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _make_executable_hook(hooks_dir: Path, name: str, content: str = "#!/bin/sh\n: noop\n") -> Path:
    """Write a hook file and mark it executable (POSIX) or just write it (Windows)."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / name
    hook.write_text(content, encoding="utf-8")
    if os.name != "nt":
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook


# ---------------------------------------------------------------------------
# Test 1 – init must NOT create .githooks/post-commit
# ---------------------------------------------------------------------------

def test_init_does_not_install_post_commit_hook(tmp_git_repo):
    """
    Plain `teamcache init` (no --enable-hooks) must not create .githooks/post-commit.
    The post-commit hook was the dangerous amend-based hook from v0.1.x; it must
    never be written again.
    """
    result = run_teamcache_init(tmp_git_repo)
    assert result.returncode == 0, f"init failed: {result.stderr}"

    post_commit = tmp_git_repo / ".githooks" / "post-commit"
    assert not post_commit.exists(), (
        ".githooks/post-commit must not be created by plain init"
    )


def test_init_enable_hooks_does_not_install_post_commit_hook(tmp_git_repo):
    """
    Even `teamcache init --enable-hooks` must not create .githooks/post-commit;
    only post-merge should be written.
    """
    result = run_teamcache_init(tmp_git_repo, ["--enable-hooks"])
    assert result.returncode == 0, f"init --enable-hooks failed: {result.stderr}"

    post_commit = tmp_git_repo / ".githooks" / "post-commit"
    assert not post_commit.exists(), (
        ".githooks/post-commit must not be created even with --enable-hooks"
    )


# ---------------------------------------------------------------------------
# Test 2 – plain init must NOT set core.hooksPath
# ---------------------------------------------------------------------------

def test_init_no_flags_does_not_set_hooksPath(tmp_git_repo):
    """
    `teamcache init` without --enable-hooks must leave git config core.hooksPath
    untouched (i.e. absent from the local config).  Setting hooksPath without
    the user's explicit consent would silently disable all their existing hooks.
    """
    result = run_teamcache_init(tmp_git_repo)
    assert result.returncode == 0, f"init failed: {result.stderr}"

    hooks_path = _git_config_get(tmp_git_repo, "core.hooksPath")
    assert hooks_path is None, (
        f"core.hooksPath must NOT be set by plain init, got: {hooks_path!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 – --enable-hooks with no conflicts installs post-merge
# ---------------------------------------------------------------------------

def test_init_enable_hooks_no_conflict_installs_post_merge(tmp_git_repo):
    """
    `teamcache init --enable-hooks` on a repo with no pre-existing hooks in
    .git/hooks/ should succeed (exit 0) and write .githooks/post-merge.
    core.hooksPath should be set to ".githooks".
    """
    # Ensure .git/hooks/ has no conflicting executable hooks
    git_hooks_dir = tmp_git_repo / ".git" / "hooks"
    # Remove any sample hooks if present (they should not trigger conflict detection)
    for sample in git_hooks_dir.glob("*.sample"):
        pass  # samples are intentionally ignored by _detect_hook_conflicts

    result = run_teamcache_init(tmp_git_repo, ["--enable-hooks"])
    assert result.returncode == 0, (
        f"init --enable-hooks should succeed with no conflicts.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    post_merge = tmp_git_repo / ".githooks" / "post-merge"
    assert post_merge.exists(), ".githooks/post-merge must exist after --enable-hooks"
    assert post_merge.is_file(), ".githooks/post-merge must be a regular file"

    hooks_path = _git_config_get(tmp_git_repo, "core.hooksPath")
    assert hooks_path == ".githooks", (
        f"core.hooksPath should be '.githooks', got: {hooks_path!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 – --enable-hooks with a conflicting hook exits non-zero
# ---------------------------------------------------------------------------

def test_init_enable_hooks_with_conflict_exits_nonzero(tmp_git_repo):
    """
    If .git/hooks/pre-commit is already executable, `teamcache init --enable-hooks`
    must exit with code 1 and print an error rather than silently overwriting.
    """
    git_hooks_dir = tmp_git_repo / ".git" / "hooks"
    _make_executable_hook(git_hooks_dir, "pre-commit")

    result = run_teamcache_init(tmp_git_repo, ["--enable-hooks"])

    # On Windows all files appear executable, so the conflict should always trigger.
    assert result.returncode != 0, (
        "init --enable-hooks must exit non-zero when a conflicting hook exists.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "conflict" in combined.lower() or "existing" in combined.lower() or "error" in combined.lower(), (
        "Output should mention the conflict or error"
    )


# ---------------------------------------------------------------------------
# Test 5 – --enable-hooks --force-hooks succeeds despite conflicts
# ---------------------------------------------------------------------------

def test_init_enable_hooks_force_succeeds(tmp_git_repo):
    """
    `teamcache init --enable-hooks --force-hooks` must succeed (exit 0) and
    install .githooks/post-merge even when there are conflicting hooks in
    .git/hooks/.
    """
    git_hooks_dir = tmp_git_repo / ".git" / "hooks"
    _make_executable_hook(git_hooks_dir, "pre-commit")
    _make_executable_hook(git_hooks_dir, "post-merge")

    result = run_teamcache_init(tmp_git_repo, ["--enable-hooks", "--force-hooks"])
    assert result.returncode == 0, (
        f"init --enable-hooks --force-hooks should succeed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    post_merge = tmp_git_repo / ".githooks" / "post-merge"
    assert post_merge.exists(), ".githooks/post-merge must exist after --force-hooks"


# ---------------------------------------------------------------------------
# Test 6 – post-merge hook content must not contain "amend"
# ---------------------------------------------------------------------------

def test_post_merge_hook_has_no_amend_line(tmp_git_repo):
    """
    The post-merge hook written by `teamcache init --enable-hooks` must not
    contain any line with the word "amend".  The amend-on-post-commit pattern
    was the P0 bug in v0.1.x; it must never reappear in any hook.
    """
    result = run_teamcache_init(tmp_git_repo, ["--enable-hooks"])
    assert result.returncode == 0, f"init --enable-hooks failed: {result.stderr}"

    post_merge = tmp_git_repo / ".githooks" / "post-merge"
    assert post_merge.exists(), ".githooks/post-merge must exist"

    content = post_merge.read_text(encoding="utf-8")
    amend_lines = [line for line in content.splitlines() if "amend" in line]
    assert not amend_lines, (
        f".githooks/post-merge must not contain 'amend'; found lines: {amend_lines}"
    )


# ---------------------------------------------------------------------------
# Test 7 – migrate-hooks strips amend lines from post-commit
# ---------------------------------------------------------------------------

def test_migrate_hooks_removes_amend_lines(tmp_git_repo):
    """
    `teamcache migrate-hooks` must strip every line containing "amend" from
    .githooks/post-commit (the dangerous hook left by v0.1.x) and leave all
    other lines intact.
    """
    # First run init to set up the .teamcache structure
    init_result = run_teamcache_init(tmp_git_repo)
    assert init_result.returncode == 0, f"init failed: {init_result.stderr}"

    # Manually create a v0.1.x-style post-commit hook with amend lines
    githooks_dir = tmp_git_repo / ".githooks"
    githooks_dir.mkdir(parents=True, exist_ok=True)
    old_hook_content = (
        "#!/bin/sh\n"
        "teamcache index --quiet\n"
        "git add .teamcache/objects/\n"
        "git commit --amend --no-edit --no-verify\n"
        "echo done\n"
    )
    post_commit = githooks_dir / "post-commit"
    post_commit.write_text(old_hook_content, encoding="utf-8")

    # Run migrate-hooks
    migrate_result = subprocess.run(
        ["python", "-m", "teamcache", "migrate-hooks"],
        cwd=tmp_git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrate_result.returncode == 0, (
        f"migrate-hooks failed.\nstdout: {migrate_result.stdout}\nstderr: {migrate_result.stderr}"
    )

    updated_content = post_commit.read_text(encoding="utf-8")
    amend_lines = [line for line in updated_content.splitlines() if "amend" in line]
    assert not amend_lines, (
        f"migrate-hooks must remove all amend lines, but found: {amend_lines}"
    )

    # Non-amend lines must be preserved
    assert "teamcache index --quiet" in updated_content, (
        "migrate-hooks must preserve non-amend lines"
    )
    assert "echo done" in updated_content, (
        "migrate-hooks must preserve non-amend lines"
    )

    # Output should mention removed lines
    combined = migrate_result.stdout + migrate_result.stderr
    assert "amend" in combined.lower() or "removed" in combined.lower(), (
        "migrate-hooks output should report what it removed"
    )
