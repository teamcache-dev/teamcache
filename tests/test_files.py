from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from teamcache.files import cache_key, file_hash, is_binary_content, should_skip_metadata


def test_file_hash_is_sha256():
    content = b"hello world"
    expected = hashlib.sha256(content).hexdigest()
    assert file_hash(content) == expected


def test_cache_key_formula():
    content = b"hello"
    fhash = file_hash(content)
    expected = hashlib.sha256(f"{fhash}|v1".encode()).hexdigest()
    assert cache_key(fhash, "v1") == expected


def test_cache_key_changes_with_schema_version():
    fhash = file_hash(b"hello")
    assert cache_key(fhash, "v1") != cache_key(fhash, "v2")


def test_is_binary_content_null_byte():
    assert is_binary_content(Path("file.txt"), b"hello\x00world") is True


def test_is_binary_content_clean_text():
    assert is_binary_content(Path("file.py"), b"def foo(): pass\n") is False


def test_is_binary_content_by_extension():
    assert is_binary_content(Path("image.png"), b"not really binary") is True


def test_should_skip_metadata_lock_file(tmp_path):
    f = tmp_path / "package-lock.json"
    f.write_bytes(b"x" * 10)
    # .json is not in SKIP_SUFFIXES; .lock is
    lock = tmp_path / "yarn.lock"
    lock.write_bytes(b"x")
    assert should_skip_metadata(lock) is True


def test_should_skip_metadata_large_file(tmp_path):
    big = tmp_path / "big.py"
    big.write_bytes(b"x" * (512 * 1024 + 1))
    assert should_skip_metadata(big) is True


def test_should_skip_metadata_normal_file(tmp_path):
    f = tmp_path / "main.py"
    f.write_bytes(b"print('hi')")
    assert should_skip_metadata(f) is False
