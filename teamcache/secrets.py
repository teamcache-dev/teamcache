"""Secret scanner — checks summaries for accidental credential leakage before caching."""

from __future__ import annotations

import re

# 30+ patterns covering common credentials. Ordered roughly by false-positive risk (lowest first).
_PATTERNS: list[re.Pattern[str]] = [p for p in [
    # Private keys (very high signal)
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"),

    # AWS
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)aws.{0,30}secret.{0,30}['\"\s=:][A-Za-z0-9/+]{40}"),

    # GitHub tokens
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"gho_[a-zA-Z0-9]{36}"),
    re.compile(r"ghs_[a-zA-Z0-9]{36}"),
    re.compile(r"ghu_[a-zA-Z0-9]{36}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{82}"),

    # OpenAI / Anthropic
    re.compile(r"sk-[a-zA-Z0-9]{48}"),
    re.compile(r"sk-ant-api[0-9]{2}-[a-zA-Z0-9\-_]{93}"),

    # Stripe
    re.compile(r"sk_live_[a-zA-Z0-9]{24,}"),
    re.compile(r"sk_test_[a-zA-Z0-9]{24,}"),
    re.compile(r"rk_live_[a-zA-Z0-9]{24,}"),

    # Slack
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,48}"),
    re.compile(r"https://hooks\.slack\.com/services/T[0-9A-Z]{8}/B[0-9A-Z]{8}/[0-9a-zA-Z]{24}"),

    # Google
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ya29\.[0-9A-Za-z\-_]{20,}"),

    # HuggingFace
    re.compile(r"hf_[a-zA-Z0-9]{34}"),

    # PyPI / npm
    re.compile(r"pypi-[A-Za-z0-9\-_]{50,}"),
    re.compile(r"npm_[A-Za-z0-9]{36}"),

    # SendGrid
    re.compile(r"SG\.[a-zA-Z0-9_\-]{22,}\.[a-zA-Z0-9_\-]{43,}"),

    # Twilio
    re.compile(r"AC[a-fA-F0-9]{32}"),

    # Databricks
    re.compile(r"dapi[a-f0-9]{32}"),

    # Cloudflare
    re.compile(r"(?i)cloudflare.{0,30}['\"\s=:][a-zA-Z0-9_\-]{40}"),

    # Heroku
    re.compile(r"(?i)heroku.{0,30}['\"\s=:][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),

    # Azure storage
    re.compile(r"DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[a-zA-Z0-9+/=]{88}"),

    # Database URLs with embedded passwords
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:/@\s]+:[^@\s]{8,}@"),

    # Generic high-entropy secrets (key/token/secret = value patterns)
    re.compile(r"(?i)(?:secret|api_?key|auth_?token|access_?token|private_?key)\s*[=:]\s*['\"]?[A-Za-z0-9+/\-_]{32,}"),

    # JWT
    re.compile(r"eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}"),

    # SSH private key passphrase markers
    re.compile(r"Proc-Type: 4,ENCRYPTED"),

    # Generic bearer tokens in headers
    re.compile(r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9\-_.~+/]{20,}"),
] if p is not None]


def contains_secret(text: str) -> bool:
    """Return True if text appears to contain a credential or secret."""
    for pattern in _PATTERNS:
        if pattern.search(text):
            return True
    return False


def first_match(text: str) -> str | None:
    """Return the pattern description of the first match, for logging."""
    for pattern in _PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None
