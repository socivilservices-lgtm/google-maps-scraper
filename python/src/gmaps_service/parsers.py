"""Parsers for Maps JSON, Maps HTML, review RPC payloads, and Places API JSON.

Index paths for the browser JSON follow ``gmaps/entry.go`` and
``gmaps/multiple.go`` so the repository fixtures keep parsing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from gmaps_service.errors import ParseError
from gmaps_service.jsonnav import load_google_json, nth, nth_float, nth_list, nth_str
from gmaps_service.models import Business, Review
from gmaps_service.normalize import (
    finalize_business,
    hours_from_day_map,
    open_now_from_status,
    region_code,
)

_EARLIEST_REVIEW = datetime(2007, 1, 1, tzinfo=timezone.utc)
_FUTURE_SKEW = timedelta(hours=24)
_PLACE_ID_RE = re.compile(r"!19s(ChIJ[A-Za-z0-9_\-]+)")
_FEATURE_ID_RE = re.compile(r"!1s([^!]+)")
_HEX_ID_RE = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
_GEO_RE = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+))")
_DAY_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(.+)$",
    re.IGNORECASE,
)
_STARS_RE = re.compile(r"([\d.]+)\s*stars?", re.IGNORECASE)
_REVIEWS_RE = re.compile(r"([\d,]+)\s+reviews?", re.IGNORECASE)
_PRICE_TOKEN_RE = re.compile(r"^[\$€£¥]{1,4}$")

_PLACES_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}
_GENERIC_TYPES = {"point_of_interest", "establishment"}


def business_from_place_payload(raw: bytes | str | Any) -> Business:
    """Parse one place document from ``APP_INITIALIZATION_STATE`` or a fixture.

    Older responses keep the place array at index 6. Newer responses wrap that
    same array deeper in the tree; ``find_place_darray`` locates it.
    """
    data = load_google_json(raw)
    darray = _classic_darray(data) or find_place_darray(data)
    if not darray:
        raise ParseError("place payload is missing the business data array")
    business = finalize_business(business_from_darray(darray))
    if not business.name:
        raise ParseError("place payload has no name")
    return business


def _classic_darray(data: Any) -> list[Any] | None:
    if not isinstance(data, list) or len(data) < 7 or not isinstance(data[6], list):
        return None
    if len(data[6]) > 11 and isinstance(data[6][11], str) and data[6][11].strip():
        return data[6]
    return None


def find_place_darray(node: Any) -> list[Any] | None:
    """Find the long place array (name at index 11, place id near index 78)."""
    best: list[Any] | None = None
    best_score = 0
    visits = 0

    def visit(current: Any, level: int) -> None:
        nonlocal best, best_score, visits
        if visits > 4000 or level > 12:
            return
        visits += 1
        if isinstance(current, dict):
            for child in current.values():
                visit(child, level + 1)
            return
        if not isinstance(current, list):
            return
        score = _darray_score(current)
        if score > best_score:
            best = current
            best_score = score
        for child in current:
            if isinstance(child, (list, dict)):
                visit(child, level + 1)

    visit(node, 0)
    return best if best_score else None


def _darray_score(node: list[Any]) -> int:
    if len(node) < 80 or len(node) <= 11 or not isinstance(node[11], str) or not node[11].strip():
        return 0
    score = 1
    if len(node) > 78 and isinstance(node[78], str) and node[78].startswith("ChIJ"):
        score += 5
    if len(node) > 10 and isinstance(node[10], str) and node[10].startswith("0x"):
        score += 2
    return score


def parse_search_results(raw: bytes | str | Any) -> list[Business]:
    """Parse a Maps search XHR body into partial business records."""
    data = load_google_json(raw)
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise ParseError("search payload is not a business list")
    items = nth_list(data[0], 1)
    if len(items) < 2:
        raise ParseError("search payload has an empty business list")
    found: list[Business] = []
    for item in items[1:]:
        if not isinstance(item, list):
            continue
        detail = nth_list(item, 14)
        if not detail:
            continue
        business = finalize_business(business_from_darray(detail))
        if business.name:
            found.append(business)
    if not found:
        raise ParseError("search payload contained no named businesses")
    return found


def parse_maps_payloads(raw: bytes | str) -> list[Business]:
    """Try a place document first, then a search-result list."""
    try:
        return [business_from_place_payload(raw)]
    except (ParseError, ValueError, TypeError):
        pass
    try:
        return parse_search_results(raw)
    except (ParseError, ValueError, TypeError):
        return []


def parse_reviews(reviews: Any) -> list[Review]:
    """Parse the review array used by ``gmaps.parseReviews`` and the RPC body."""
    if isinstance(reviews, (bytes, str)):
        reviews = load_google_json(reviews)
    if not isinstance(reviews, list):
        return []
    parsed: list[Review] = []
    for item in reviews:
        element = item[0] if isinstance(item, list) and item and isinstance(item[0], list) else item
        if not isinstance(element, list):
            continue
        review = _review_from_element(element)
        if review is not None:
            parsed.append(review)
    return parsed


def parse_reviews_rpc(raw: bytes | str | Any) -> list[Review]:
    """Parse a ``listugcposts`` style body. Reviews usually sit at index 2."""
    try:
        data = load_google_json(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    reviews = nth_list(data, 2) or nth_list(data, 0)
    return parse_reviews(reviews)


def business_from_darray(darray: list[Any]) -> Business:
    name = nth_str(darray, 11).strip()
    address = nth_str(darray, 18).strip()
    if address.startswith(name + ","):
        address = address[len(name) + 1 :].strip()
    if not address:
        parts = [str(part).strip() for part in nth_list(darray, 2) if part not in (None, "")]
        address = ", ".join(parts)
    raw_categories = nth_list(darray, 13)
    categories = [item.strip() for item in raw_categories if isinstance(item, str) and item.strip()]
    status = nth_str(darray, 34, 4, 4) or nth_str(darray, 88, 0)
    open_now = open_now_from_status(status)
    day_map = _hours(darray)
    lat = nth_float(darray, 9, 2)
    lng = nth_float(darray, 9, 3)
    if lat == 0 and lng == 0:
        lat_value = None
        lng_value = None
    else:
        lat_value = lat
        lng_value = lng
    rating_raw = nth(darray, 4, 7)
    count_raw = nth(darray, 4, 8)
    price_raw = nth(darray, 4, 2)
    owner_id = nth_str(darray, 57, 2)
    country = nth_str(darray, 183, 1, 6)
    reviews_node = nth_list(darray, 175, 9, 0, 0) or nth_list(darray, 175, 9, 0)
    return Business(
        name=name,
        address=address,
        phone=nth_str(darray, 178, 0, 0) or None,
        website=nth_str(darray, 7, 0) or None,
        rating=float(rating_raw) if isinstance(rating_raw, (int, float)) and not isinstance(rating_raw, bool) else None,
        review_count=int(count_raw) if isinstance(count_raw, (int, float)) and not isinstance(count_raw, bool) else None,
        latitude=lat_value,
        longitude=lng_value,
        categories=categories,
        opening_hours=hours_from_day_map(day_map, open_now),
        open_now=open_now,
        price_level=_price_level(price_raw),
        claimed=True if owner_id else None,
        reviews=parse_reviews(reviews_node),
        place_id=nth_str(darray, 78) or nth_str(darray, 10) or None,
        maps_url=nth_str(darray, 27) or None,
        region=region_code(country),
    )


def parse_place_html(html: str, page_url: str = "") -> Business:
    """DOM fallback for a Maps place panel. Selectors are layered, not single-class."""
    soup = BeautifulSoup(html or "", "html.parser")
    name = ""
    heading = soup.select_one("h1")
    if heading is not None:
        name = heading.get_text(" ", strip=True)
    address = _button_text(soup, 'button[data-item-id="address"]', "Address:")
    phone = _button_text(soup, 'button[data-item-id^="phone:"]', "Phone:")
    if not phone:
        phone_node = soup.select_one('button[data-item-id^="phone:"]')
        if phone_node is not None:
            item_id = phone_node.get("data-item-id") or ""
            phone = item_id.split("phone:tel:", 1)[-1] or None
    website = None
    site = soup.select_one('a[data-item-id="authority"], a[aria-label^="Website:"]')
    if site is not None:
        website = site.get("href")
    categories: list[str] = []
    for button in soup.select("button"):
        action = button.get("jsaction") or ""
        classes = " ".join(button.get("class") or [])
        if "category" in action or "DkEaL" in classes:
            label = button.get_text(" ", strip=True)
            if label:
                categories.append(label)
    rating = None
    review_count = None
    for node in soup.find_all(attrs={"aria-label": True}):
        label = node.get("aria-label") or ""
        stars = _STARS_RE.search(label)
        if stars and rating is None:
            rating = float(stars.group(1))
        reviews = _REVIEWS_RE.search(label)
        if reviews and review_count is None:
            review_count = int(reviews.group(1).replace(",", ""))
    open_now = _open_now_from_dom(soup)
    day_map = _hours_from_dom(soup)
    lat, lng = _geo_from_url(page_url)
    if lat is None:
        for anchor in soup.select("a[href]"):
            lat, lng = _geo_from_url(anchor.get("href") or "")
            if lat is not None:
                break
    price_level = None
    for node in soup.find_all(attrs={"aria-label": True}):
        label = node.get("aria-label") or ""
        if "price" in label.lower():
            token = label.split(":")[-1].strip()
            if _PRICE_TOKEN_RE.match(token):
                price_level = token
                break
    if price_level is None:
        for text in soup.stripped_strings:
            if _PRICE_TOKEN_RE.match(text):
                price_level = text
                break
    claimed = _claimed_from_dom(soup)
    place_id = extract_place_id(page_url)
    return finalize_business(Business(
        name=name,
        address=address or "",
        phone=phone,
        website=website,
        rating=rating,
        review_count=review_count,
        latitude=lat,
        longitude=lng,
        categories=categories,
        opening_hours=hours_from_day_map(day_map, open_now),
        open_now=open_now,
        price_level=price_level,
        claimed=claimed,
        reviews=parse_reviews_html(html),
        place_id=place_id,
        maps_url=page_url or None,
    ))


def parse_reviews_html(html: str) -> list[Review]:
    soup = BeautifulSoup(html or "", "html.parser")
    cards = soup.select("div.jftiEf") or soup.select("[data-review-id]")
    reviews: list[Review] = []
    for card in cards:
        author_node = card.select_one(".d4r55") or card.select_one("[class*='d4r55']")
        author = author_node.get_text(" ", strip=True) if author_node else ""
        if not author:
            image = card.select_one('[role="img"][aria-label]')
            # Author is often the first short text node.
            for text in card.stripped_strings:
                if text and "star" not in text.lower() and not text[:1].isdigit():
                    author = text
                    break
            if image and not author:
                author = ""
        text_node = card.select_one(".wiI7pd") or card.select_one("[class*='wiI7pd']")
        text = text_node.get_text(" ", strip=True) if text_node else ""
        date_node = card.select_one(".rsqaWe") or card.select_one("[class*='rsqaWe']")
        date = date_node.get_text(" ", strip=True) if date_node else None
        rating = None
        star = card.select_one('[role="img"][aria-label*="star"]')
        if star is not None:
            match = _STARS_RE.search(star.get("aria-label") or "")
            if match:
                rating = float(match.group(1))
        if author or text:
            reviews.append(Review(text=text, author=author, rating=rating, date=date))
    return reviews


def business_from_places(place: dict[str, Any]) -> Business:
    """Map one Places API (New) place resource onto ``Business``."""
    display = place.get("displayName")
    if isinstance(display, dict):
        name = str(display.get("text") or "").strip()
    else:
        name = str(display or "").strip()
    phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
    location = place.get("location") or {}
    lat = location.get("latitude")
    lng = location.get("longitude")
    categories = []
    for item in place.get("types") or []:
        if not isinstance(item, str) or item in _GENERIC_TYPES:
            continue
        categories.append(item.replace("_", " ").strip().title())
    hours = _hours_from_places(place.get("currentOpeningHours"), place.get("regularOpeningHours"))
    open_now = hours.get("open_now")
    if not isinstance(open_now, bool):
        open_now = None
    price_raw = place.get("priceLevel")
    price_level = _PRICE_LEVELS.get(price_raw) if isinstance(price_raw, str) else price_raw
    reviews = [_review_from_places(item) for item in place.get("reviews") or []]
    reviews = [review for review in reviews if review is not None]
    place_id = str(place.get("id") or "")
    if isinstance(place_id, str) and place_id.startswith("places/"):
        place_id = place_id.split("/", 1)[1]
    return finalize_business(
        Business(
            name=name,
            address=str(place.get("formattedAddress") or ""),
            phone=str(phone) if phone else None,
            website=place.get("websiteUri"),
            rating=float(place["rating"]) if isinstance(place.get("rating"), (int, float)) else None,
            review_count=int(place["userRatingCount"]) if isinstance(place.get("userRatingCount"), int) else None,
            latitude=float(lat) if isinstance(lat, (int, float)) else None,
            longitude=float(lng) if isinstance(lng, (int, float)) else None,
            categories=categories,
            opening_hours=hours,
            open_now=open_now,
            price_level=price_level,
            claimed=None,
            reviews=reviews,
            place_id=place_id or None,
            maps_url=place.get("googleMapsUri"),
            enriched=True,
        )
    )


def extract_place_id(map_url: str | None) -> str | None:
    """Prefer the canonical ``ChIJ`` place id, then the ``!1s`` feature id."""
    if not map_url:
        return None
    parsed = urlparse(map_url)
    query_id = parse_qs(parsed.query).get("place_id", [""])[0]
    if query_id:
        return unquote(query_id)
    query_place = parse_qs(parsed.query).get("query_place_id", [""])[0]
    if query_place:
        return unquote(query_place)
    match = _PLACE_ID_RE.search(map_url)
    if match:
        return match.group(1)
    match = _FEATURE_ID_RE.search(map_url)
    if match:
        return unquote(match.group(1))
    match = _HEX_ID_RE.search(map_url)
    if match:
        return match.group(0)
    return None


def _hours(darray: list[Any]) -> dict[str, list[str]]:
    items = nth_list(darray, 203, 0) or nth_list(darray, 34, 1)
    hours: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, list):
            continue
        day = nth_str(item, 0)
        if not day:
            continue
        slots = nth_list(item, 3)
        times: list[str] = []
        if slots:
            for slot in slots:
                if isinstance(slot, list) and slot and isinstance(slot[0], str):
                    times.append(slot[0])
                elif isinstance(slot, str):
                    times.append(slot)
        if not times:
            for value in nth_list(item, 1):
                if isinstance(value, str) and value:
                    times.append(value)
        if times:
            hours[day] = times
    return hours


def _price_level(value: Any) -> str | int | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _review_from_element(element: list[Any]) -> Review | None:
    author = _author_name(element)
    if not author:
        return None
    rating_float = _rating_value(element)
    text = _first_str(
        nth_str(element, 2, 15, 0, 0),
        nth_str(element, 2, 15, 1, 0),
        nth_str(element, 2, 15, 0),
        nth_str(element, 3, 0),
    )
    posted = nth_float(element, 1, 2) or nth_float(element, 1, 3)
    relative = _first_str(
        nth_str(element, 1, 6),
        nth_str(element, 3, 3),
        nth_str(element, 2, 1, 3, 8, 0),
    )
    return Review(
        text=text,
        author=author,
        rating=rating_float,
        date=_timestamp_to_iso(posted) or relative or None,
    )


def _author_name(element: list[Any]) -> str:
    return _first_str(
        nth_str(element, 1, 4, 5, 0),
        nth_str(element, 1, 4, 4),
        nth_str(element, 1, 4, 0, 4),
        nth_str(element, 1, 4, 1, 1),
        nth_str(element, 1, 4, 2, 1),
        nth_str(element, 0, 1),
    )


def _rating_value(element: list[Any]) -> float | None:
    body = nth_list(element, 2)
    if body and body[0] is None:
        value = nth_float(element, 2, 8, 1)
        return value or None
    value = nth_float(element, 2, 0, 0) or nth_float(element, 2, 0) or nth_float(element, 1, 0, 0)
    return value or None


def _timestamp_to_iso(value: float) -> str | None:
    if not value:
        return None
    seconds = float(value)
    if seconds > 1e14:
        seconds /= 1_000_000
    elif seconds > 1e11:
        seconds /= 1_000
    try:
        published = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if published < _EARLIEST_REVIEW:
        return None
    if published > datetime.now(timezone.utc) + _FUTURE_SKEW:
        return None
    return published.isoformat()


def _first_str(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def _button_text(soup: BeautifulSoup, selector: str, label_prefix: str) -> str | None:
    node = soup.select_one(selector)
    if node is None:
        return None
    aria = node.get("aria-label") or ""
    if aria.lower().startswith(label_prefix.lower()):
        return aria.split(":", 1)[-1].strip() or None
    text = node.get_text(" ", strip=True)
    return text or None


def _open_now_from_dom(soup: BeautifulSoup) -> bool | None:
    main = soup.select_one('[role="main"]') or soup
    for text in main.stripped_strings:
        cleaned = text.strip()
        if len(cleaned) > 80:
            continue
        status = open_now_from_status(cleaned)
        if status is not None and len(cleaned.split()) <= 8:
            return status
    return None


def _hours_from_dom(soup: BeautifulSoup) -> dict[str, list[str]]:
    hours: dict[str, list[str]] = {}
    for node in soup.find_all(attrs={"aria-label": True}):
        label = re.sub(r",?\s*copy open hours$", "", (node.get("aria-label") or "").strip(), flags=re.IGNORECASE)
        match = _DAY_RE.match(label)
        if not match:
            continue
        day = match.group(1).title()
        hours.setdefault(day, []).append(match.group(2).strip())
    return hours


def _claimed_from_dom(soup: BeautifulSoup) -> bool | None:
    text = soup.get_text(" ", strip=True)
    if re.search(r"claim this business", text, re.IGNORECASE):
        return False
    if re.search(r"\bclaimed\b", text, re.IGNORECASE):
        return True
    return None


def _geo_from_url(url: str) -> tuple[float | None, float | None]:
    match = _GEO_RE.search(url or "")
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def _hours_from_places(current: Any, regular: Any) -> dict[str, object]:
    source = current if isinstance(current, dict) and current else {}
    fallback = regular if isinstance(regular, dict) else {}
    open_now = source.get("openNow", fallback.get("openNow"))
    if not isinstance(open_now, bool):
        open_now = None
    weekday = source.get("weekdayDescriptions") or fallback.get("weekdayDescriptions") or []
    periods_raw = source.get("periods") or fallback.get("periods") or []
    periods: list[dict[str, object]] = []
    for period in periods_raw:
        if not isinstance(period, dict):
            continue
        opened = period.get("open") or {}
        closed = period.get("close") or {}
        day_index = opened.get("day") if isinstance(opened, dict) else None
        day = _PLACES_DAYS[day_index] if isinstance(day_index, int) and 0 <= day_index < 7 else None
        periods.append({"day": day, "open": _clock(opened), "close": _clock(closed)})
    return {"open_now": open_now, "weekday_text": list(weekday), "periods": periods}


def _clock(part: Any) -> str | None:
    if not isinstance(part, dict) or "hour" not in part:
        return None
    hour = int(part.get("hour") or 0)
    minute = int(part.get("minute") or 0)
    return f"{hour:02d}:{minute:02d}"


def _review_from_places(item: Any) -> Review | None:
    if not isinstance(item, dict):
        return None
    author = ""
    attribution = item.get("authorAttribution") or {}
    if isinstance(attribution, dict):
        author = str(attribution.get("displayName") or "")
    text_obj = item.get("originalText") or item.get("text") or {}
    text = text_obj.get("text") if isinstance(text_obj, dict) else str(text_obj or "")
    rating = float(item["rating"]) if isinstance(item.get("rating"), (int, float)) else None
    date = item.get("publishTime") or item.get("relativePublishTimeDescription")
    if not author and not text:
        return None
    return Review(text=str(text or ""), author=author, rating=rating, date=str(date) if date else None)
