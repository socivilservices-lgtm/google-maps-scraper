"""Index helpers for the nested arrays Google Maps embeds in responses.

The layout matches the Go scraper in ``gmaps/entry.go``. Missing indexes and
type mismatches yield empty defaults instead of raising, because Google omits
large parts of the tree from one response to the next.
"""

from __future__ import annotations

import json
from typing import Any


def load_google_json(raw: bytes | str | Any) -> Any:
    """Parse a Maps payload, including the XSSI prefix ``)]}'``."""
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = text.strip()
    if text.startswith(")]}'"):
        text = text[4:].lstrip()
    data = json.loads(text)
    if isinstance(data, str):
        nested = data.strip()
        if nested.startswith(")]}'"):
            nested = nested[4:].lstrip()
        if nested[:1] in "[{":
            data = json.loads(nested)
    return data


def nth(node: Any, *indexes: int) -> Any:
    current = node
    for index in indexes:
        if isinstance(current, (str, bytes)) or not isinstance(current, list):
            return None
        if index < 0 or index >= len(current):
            return None
        current = current[index]
    return current


def nth_str(node: Any, *indexes: int) -> str:
    value = nth(node, *indexes)
    return value if isinstance(value, str) else ""


def nth_float(node: Any, *indexes: int) -> float:
    value = nth(node, *indexes)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def nth_list(node: Any, *indexes: int) -> list[Any]:
    value = nth(node, *indexes)
    return value if isinstance(value, list) else []
