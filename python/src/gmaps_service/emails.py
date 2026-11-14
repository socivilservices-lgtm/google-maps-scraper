"""Public emails from a business's own homepage and one contact page.

Only the site linked as the official website is fetched. Social profiles are
rejected before this module runs.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from gmaps_service.logging_utils import EventLogger, get_logger

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
CONTACT_HINTS = ("contact", "kontakt", "impressum", "about")
IGNORED_DOMAINS = {
    "example.com",
    "example.org",
    "sentry.io",
    "wixpress.com",
    "schema.org",
    "godaddy.com",
    "cloudflare.com",
    "googleapis.com",
    "google.com",
    "gstatic.com",
}
IGNORED_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply"}
_MAX_BYTES = 1_000_000


def extract_emails_from_html(html: str) -> list[str]:
    """Collect mailto targets first, then visible addresses in the HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    for anchor in soup.select('a[href^="mailto:"]'):
        href = anchor.get("href") or ""
        address = href.split(":", 1)[-1].split("?", 1)[0]
        found.extend(_split_addresses(address))
    text = soup.get_text(" ", strip=True)
    found.extend(EMAIL_RE.findall(text))
    return _clean_emails(found)


def find_contact_url(html: str, base_url: str) -> str | None:
    """Return the first same-host contact-like link, if the page has one."""
    soup = BeautifulSoup(html or "", "html.parser")
    base = urlparse(base_url)
    base_host = (base.hostname or "").lower().removeprefix("www.")
    if not base_host:
        return None
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        label = f"{href} {anchor.get_text(' ', strip=True)}".lower()
        if not any(hint in label for hint in CONTACT_HINTS):
            continue
        absolute = urljoin(base_url, href)
        host = (urlparse(absolute).hostname or "").lower().removeprefix("www.")
        if host != base_host:
            continue
        if urlparse(absolute).path.rstrip("/") == urlparse(base_url).path.rstrip("/"):
            continue
        return absolute.split("#", 1)[0]
    return None


async def fetch_public_emails(
    website: str,
    *,
    client: httpx.AsyncClient | None = None,
    logger: EventLogger | None = None,
    proxy: str | None = None,
) -> list[str]:
    """Fetch the homepage and, when linked, a single contact page."""
    log = logger or get_logger()
    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": "gmaps-service/0.1 (+https://github.com/gosom/google-maps-scraper)"},
    )
    try:
        homepage, final_url = await _get_html(http, website, log)
        emails = extract_emails_from_html(homepage)
        contact = find_contact_url(homepage, final_url or website)
        if contact and contact.rstrip("/") != (final_url or website).rstrip("/"):
            contact_html, _ = await _get_html(http, contact, log)
            emails.extend(extract_emails_from_html(contact_html))
        return _clean_emails(emails)
    finally:
        if owns_client:
            await http.aclose()


async def _get_html(client: httpx.AsyncClient, url: str, logger: EventLogger) -> tuple[str, str]:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.error("email page fetch failed", url=url, error_type="email", detail=str(exc))
        return "", url
    if response.status_code >= 400:
        logger.error(
            "email page returned an error status",
            url=str(response.url),
            error_type="email",
            status=response.status_code,
        )
        return "", str(response.url)
    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type and "text/" not in content_type:
        return "", str(response.url)
    return response.text[:_MAX_BYTES], str(response.url)


def _split_addresses(value: str) -> list[str]:
    return EMAIL_RE.findall(value.replace("%40", "@"))


def _clean_emails(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = value.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        local, _, domain = email.partition("@")
        if local in IGNORED_LOCAL_PARTS or domain in IGNORED_DOMAINS:
            continue
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")):
            continue
        if email in seen:
            continue
        seen.add(email)
        ordered.append(email)
    return ordered
