"""Detect Google CAPTCHA and unusual-traffic interstitials.

Nothing in this module, or elsewhere in the package, submits a CAPTCHA to a
solving service. Headless jobs stop. Headed jobs wait for a person.
"""

from __future__ import annotations

import asyncio
import time

from gmaps_service.errors import CAPTCHA_MESSAGE, CaptchaDetectedError
from gmaps_service.logging_utils import EventLogger

_PHRASES = (
    "our systems have detected unusual traffic",
    "detected unusual traffic from your computer network",
    "unusual traffic from your computer",
)


def looks_like_captcha(url: str | None, html: str = "", title: str = "") -> bool:
    """Return True only for Google's block page, not for the word in a review."""
    location = (url or "").lower()
    if "/sorry/" in location or "google.com/sorry" in location:
        return True
    if "unusual traffic" in (title or "").lower():
        return True
    sample = (html or "")[:50000].lower()
    return any(phrase in sample for phrase in _PHRASES)


async def ensure_not_blocked(page: object, config: object, logger: EventLogger) -> None:
    """Stop a headless job, or wait for a manual solve when a window is visible."""
    url = getattr(page, "url", "") or ""
    html = await page.content()
    title = await page.title()
    if not looks_like_captcha(url, html, title):
        return
    logger.error(
        "CAPTCHA or unusual-traffic page detected",
        url=url,
        error_type="captcha",
        headless=bool(getattr(config, "headless", True)),
    )
    if getattr(config, "headless", True):
        pause = min(float(getattr(config, "delay_max", 0) or 0), 5.0)
        if pause > 0:
            await asyncio.sleep(pause)
        raise CaptchaDetectedError(CAPTCHA_MESSAGE)
    await _wait_for_manual_clear(page, config, logger)


async def _wait_for_manual_clear(page: object, config: object, logger: EventLogger) -> None:
    timeout = float(getattr(config, "manual_captcha_timeout", 180) or 180)
    logger.warning(
        "waiting for a manual CAPTCHA solve in the visible browser",
        url=getattr(page, "url", None),
        type="captcha",
        timeout_seconds=timeout,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        html = await page.content()
        title = await page.title()
        url = getattr(page, "url", "") or ""
        if not looks_like_captcha(url, html, title):
            logger.info("CAPTCHA cleared; resuming", url=url, type="captcha")
            return
    logger.error("manual CAPTCHA wait timed out", url=getattr(page, "url", None), error_type="captcha")
    raise CaptchaDetectedError(CAPTCHA_MESSAGE)
