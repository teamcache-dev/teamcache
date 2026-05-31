from __future__ import annotations


def normalize_fts_query(query: str) -> str:
    tokens = query.strip().split()
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens if token)
