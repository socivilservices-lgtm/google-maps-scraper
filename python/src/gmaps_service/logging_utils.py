"""Structured logs for scrape failures.

Error events always include a timestamp, a URL (when one is known), and a
type so operators can filter CAPTCHA, HTTP, and parse failures.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import TextIO
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_SECRET_QUERY_KEYS = {"key", "api_key", "x-goog-api-key", "access_token"}


def redact_url(url: str | None) -> str | None:
    """Drop credential-like query parameters before writing a URL to a log."""
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _SECRET_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


class EventLogger:
    """Write one JSON object per line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    def event(self, level: str, message: str, **fields: object) -> None:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        for key, value in fields.items():
            if value is not None:
                payload[key] = value
        self.stream.write(json.dumps(payload, default=str) + "\n")
        self.stream.flush()

    def info(self, message: str, **fields: object) -> None:
        self.event("info", message, **fields)

    def warning(self, message: str, **fields: object) -> None:
        self.event("warning", message, **fields)

    def error(self, message: str, *, url: str | None = None, error_type: str = "error", **fields: object) -> None:
        self.event(
            "error",
            message,
            url=redact_url(url),
            type=error_type,
            **fields,
        )


def get_logger(stream: TextIO | None = None) -> EventLogger:
    return EventLogger(stream=stream)
