"""Field cleanup shared by the browser parser and the Places API."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from gmaps_service.models import Business, Review

try:
    import phonenumbers
except ImportError:  # pragma: no cover - declared in requirements
    phonenumbers = None  # type: ignore[assignment]


SOCIAL_HOSTS = {
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "snapchat.com",
}

REGION_ALIASES = {
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "CYPRUS": "CY",
    "GREECE": "GR",
    "GERMANY": "DE",
    "FRANCE": "FR",
    "ITALY": "IT",
    "SPAIN": "ES",
    "CANADA": "CA",
    "AUSTRALIA": "AU",
    "NEW ZEALAND": "NZ",
    "IRELAND": "IE",
    "NETHERLANDS": "NL",
}

_OPEN_RE = re.compile(r"^\s*open\b", re.IGNORECASE)
_CLOSED_RE = re.compile(r"^\s*closed\b", re.IGNORECASE)


def region_code(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().upper()
    if text in REGION_ALIASES:
        return REGION_ALIASES[text]
    if len(text) == 2 and text.isalpha():
        return text
    return ""


def open_now_from_status(status: str | None) -> bool | None:
    if not status:
        return None
    if _OPEN_RE.match(status):
        return True
    if _CLOSED_RE.match(status):
        return False
    return None


def extract_actual_url(google_url: str) -> str:
    """Unwrap Google ``/url?q=`` redirects used on Maps website links."""
    if not google_url:
        return ""
    candidate = google_url.strip()
    if candidate.startswith("/url?"):
        candidate = "https://www.google.com" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host.endswith("google.com") and parsed.path.startswith("/url"):
        target = parse_qs(parsed.query).get("q", [""])[0]
        if target:
            return target
    return google_url.strip()


def is_social_host(host: str) -> bool:
    name = host.lower().removeprefix("www.")
    return any(name == social or name.endswith("." + social) for social in SOCIAL_HOSTS)


def normalize_website(url: str | None) -> str | None:
    """Keep an official http(s) website and drop social or Google links."""
    if url is None:
        return None
    actual = extract_actual_url(str(url).strip())
    if not actual:
        return None
    if actual.startswith("//"):
        actual = "https:" + actual
    if not actual.startswith(("http://", "https://")):
        actual = "https://" + actual
    parsed = urlparse(actual)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    if host.endswith("google.com") or host.endswith("goo.gl") or host.endswith("googleusercontent.com"):
        return None
    if is_social_host(host):
        return None
    scheme = parsed.scheme.lower() if parsed.scheme else "https"
    return urlunparse(parsed._replace(scheme=scheme, fragment=""))


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Return E.164 when the number can be parsed, otherwise a cleaned original."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    region_hint = region_code(region) or None
    if phonenumbers is not None:
        try:
            parsed = phonenumbers.parse(text, region_hint)
        except phonenumbers.NumberParseException:
            parsed = None
        if parsed is not None and phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("00") and len(digits) > 4:
        digits = "+" + digits[2:]
    if digits.startswith("+") and len(re.sub(r"\D", "", digits)) >= 8:
        return "+" + re.sub(r"\D", "", digits)
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


def hours_from_day_map(day_map: dict[str, list[str]], open_now: bool | None) -> dict[str, object]:
    weekday_text: list[str] = []
    periods: list[dict[str, object]] = []
    for day, slots in day_map.items():
        clean_slots = [slot for slot in slots if slot]
        if not clean_slots:
            continue
        joined = ", ".join(clean_slots)
        weekday_text.append(f"{day}: {joined}")
        periods.append({"day": day, "text": joined})
    return {"open_now": open_now, "weekday_text": weekday_text, "periods": periods}


def trim_reviews(reviews: list[Review], limit: int) -> list[Review]:
    """Keep the first ``limit`` reviews. Maps and Places already return newest first."""
    if limit < 1:
        return []
    kept: list[Review] = []
    seen: set[tuple[str, str]] = set()
    for review in reviews:
        if not review.author and not review.text:
            continue
        key = (review.author.strip().lower(), review.text.strip())
        if key in seen:
            continue
        seen.add(key)
        kept.append(review)
        if len(kept) >= limit:
            break
    return kept


def finalize_business(
    business: Business,
    *,
    default_region: str = "",
    max_reviews: int = 5,
) -> Business:
    """Normalize phone, website, hours, and review length in place."""
    business.name = (business.name or "").strip()
    business.address = re.sub(r"\s+", " ", (business.address or "")).strip(" ,")
    region = region_code(business.region) or region_code(default_region)
    business.region = region
    business.phone = normalize_phone(business.phone, region or None)
    business.website = normalize_website(business.website)
    business.categories = [category.strip() for category in business.categories if category and category.strip()]
    business.reviews = trim_reviews(business.reviews, max_reviews)
    business.emails = _unique(business.emails)
    if business.latitude == 0 and business.longitude == 0:
        business.latitude = None
        business.longitude = None
    if business.rating is not None and not 0 <= business.rating <= 10:
        business.rating = None
    if business.review_count is not None and business.review_count < 0:
        business.review_count = None
    hours = dict(business.opening_hours or {})
    if "open_now" not in hours:
        hours["open_now"] = business.open_now
    elif business.open_now is None:
        open_now = hours.get("open_now")
        business.open_now = open_now if isinstance(open_now, bool) else None
    hours["open_now"] = business.open_now
    business.opening_hours = hours
    return business


def is_usable(business: Business) -> bool:
    if not business.name:
        return False
    if business.latitude is not None and not -90 <= business.latitude <= 90:
        return False
    if business.longitude is not None and not -180 <= business.longitude <= 180:
        return False
    return True


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered
