"""Exponential backoff for HTTP 429 and 5xx responses."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from gmaps_service.errors import ScraperError
from gmaps_service.logging_utils import EventLogger

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableHTTPError(ScraperError):
    """A response status that is worth retrying."""

    def __init__(self, status: int, url: str, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after


def retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        seconds = float(header_value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


async def async_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int,
    base_delay: float,
    logger: EventLogger,
    url: str | None = None,
) -> T:
    """Run ``operation`` and retry retryable HTTP failures.

    ``retries`` is the number of attempts after the first one. Delay doubles
    each time and a short random jitter is added. A ``Retry-After`` value on
    a 429 replaces the computed delay for that attempt.
    """
    delay = max(base_delay, 0.0)
    attempt = 0
    while True:
        try:
            return await operation()
        except RetryableHTTPError as exc:
            if attempt >= retries:
                logger.error(
                    "request failed after retries",
                    url=exc.url,
                    error_type="http",
                    status=exc.status,
                    attempt=attempt,
                )
                raise
            sleep_for = exc.retry_after if exc.retry_after is not None else delay
            sleep_for += random.uniform(0, 0.25) if sleep_for else 0
            logger.warning(
                "retrying request",
                url=exc.url or url,
                type="http",
                status=exc.status,
                attempt=attempt,
                sleep_seconds=round(sleep_for, 3),
            )
            if sleep_for:
                await asyncio.sleep(sleep_for)
            delay = delay * 2 if delay else base_delay
            attempt += 1
