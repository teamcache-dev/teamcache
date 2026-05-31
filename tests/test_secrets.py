from __future__ import annotations

import pytest

from teamcache.secrets import contains_secret, first_match


@pytest.mark.parametrize("text", [
    "AKIAIOSFODNN7EXAMPLE",                                   # AWS key ID
    "-----BEGIN RSA PRIVATE KEY-----",                        # RSA private key
    "-----BEGIN OPENSSH PRIVATE KEY-----",                    # SSH private key
    "ghp_1234567890abcdefghijklmnopqrstuvwxyz12",             # GitHub PAT
    "sk-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV",    # OpenAI key (48 chars)
    "sk_live_abcdefghijklmnopqrstuvwx",                       # Stripe live
    "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuv",  # Slack bot token
    "AIzaSyD-abcdefghijklmnopqrstuvwxyz123456",               # Google API key
    "SG.abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ",  # SendGrid (43 chars after 2nd dot)
    "postgres://user:supersecretpassword@localhost/db",       # DB URL with password
    "api_key = abcdefghijklmnopqrstuvwxyzABCDEFGH",           # generic api_key
    "secret: my-very-long-secret-value-here-1234567",         # generic secret
    "hf_abcdefghijklmnopqrstuvwxyzABCDEFGH",                  # HuggingFace
    "pypi-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXY",  # PyPI token
    "ACabcdef1234567890abcdef1234567890ab",                    # Twilio SID
    "dapiabcdef1234567890abcdef1234567890",                    # Databricks (32 hex chars after dapi)
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abc123defghij",  # JWT
])
def test_detects_secret(text):
    assert contains_secret(text), f"Expected secret detected in: {text!r}"
    assert first_match(text) is not None


@pytest.mark.parametrize("text", [
    "This is a normal summary of an authentication module.",
    "imports: os, sys | classes: AuthMiddleware | functions: validate",
    "The cache_key is computed as sha256(file_hash + schema_version).",
    "Handles user login with username and password validation.",
    "def process_request(self, request): return self.handler(request)",
])
def test_clean_text_not_flagged(text):
    assert not contains_secret(text), f"False positive on: {text!r}"
