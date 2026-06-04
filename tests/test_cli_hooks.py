import subprocess
import os
import json
import datetime
from pathlib import Path
import pytest
from click.testing import CliRunner

from teamcache.cli import cli
from teamcache.config import load_config
from teamcache.store import make_store
from teamcache.db import connect, insert_summary
from teamcache.constants import INDEX_DB, LOCAL_DIR
from teamcache.files import file_hash, cache_key

@pytest.fixture()
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)
    (tmp_path / "file1.py").write_text("def test(): pass\n")
    (tmp_path / "file2.py").write_text("def prod(): pass\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)
    return tmp_path

def add_ai_summary(repo: Path, filepath: str, summary: str = "test summary"):
    config = load_config(repo)
    store = make_store(config, repo)
    conn = connect(repo / INDEX_DB)
    
    path = repo / filepath
    content = path.read_bytes()
    fhash = file_hash(content)
    key = cache_key(fhash, config.schema_version)
    
    obj = {
        "cache_key": key,
        "file_path": filepath,
        "file_hash": fhash,
        "summary": summary,
        "summary_type": "ai",
        "schema_version": config.schema_version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_by": "test",
        "file_size_bytes": len(content),
        "language": "python",
    }
    store.write(key, obj)
    insert_summary(conn, obj)
    conn.commit()
    conn.close()

def test_check_cached(repo):
    runner = CliRunner()
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])
    
    # file1.py has no AI summary (it's either unindexed or statically indexed)
    result = runner.invoke(cli, ["check-cached", "file1.py"])
    assert result.exit_code == 1
    assert "No AI summary" in result.output

    # Add AI summary and verify success
    add_ai_summary(repo, "file1.py")
    result = runner.invoke(cli, ["check-cached", "file1.py"])
    assert result.exit_code == 0
    assert result.output == ""

def test_session_uncached(repo):
    runner = CliRunner()
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])

    reads_file = repo / LOCAL_DIR / "session_reads.json"
    reads_file.parent.mkdir(parents=True, exist_ok=True)
    
    # We read file1.py and file2.py
    reads_file.write_text(json.dumps(["file1.py", "file2.py"]))

    # Both are uncached
    result = runner.invoke(cli, ["session-uncached"])
    assert result.exit_code == 0
    assert "file1.py" in result.output
    assert "file2.py" in result.output

    # Now add AI summary for file1.py
    add_ai_summary(repo, "file1.py")
    
    # Only file2.py should be output
    result = runner.invoke(cli, ["session-uncached", "--prompt"])
    assert result.exit_code == 0
    assert "file1.py" not in result.output
    assert "- file2.py" in result.output

def test_pr_check(repo):
    runner = CliRunner()
    os.chdir(repo)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["index"])

    # Modify file1.py
    (repo / "file1.py").write_text("def test(): return 1\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "update"], check=True)

    # pr-check should fail since file1.py is modified but has no AI summary
    result = runner.invoke(cli, ["pr-check", "--since", "HEAD~1"])
    assert result.exit_code == 1
    assert "missing AI summaries" in result.output
    assert "file1.py" in result.output

    # Add AI summary to file1.py
    add_ai_summary(repo, "file1.py")

    # Now it should succeed
    result = runner.invoke(cli, ["pr-check", "--since", "HEAD~1"])
    assert result.exit_code == 0
    assert "All changed files have AI summaries." in result.output
