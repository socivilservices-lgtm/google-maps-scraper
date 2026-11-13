"""Dedupe listings by place id, then by coordinates plus name."""

from __future__ import annotations

import re

from gmaps_service.models import Business, Review

# About 11 meters. Stacked businesses with different place ids stay separate.
_COORD_DIGITS = 4


def _norm_name(value: str) -> str:
    text = value.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _coord_key(business: Business) -> tuple[float, float, str] | None:
    if business.latitude is None or business.longitude is None or not business.name:
        return None
    return (round(business.latitude, _COORD_DIGITS), round(business.longitude, _COORD_DIGITS), _norm_name(business.name))


def _fill(primary: Business, extra: Business) -> None:
    """Fill empty fields on ``primary`` from ``extra`` and union contact data."""
    if not primary.address and extra.address:
        primary.address = extra.address
    if not primary.phone and extra.phone:
        primary.phone = extra.phone
    if not primary.website and extra.website:
        primary.website = extra.website
    if primary.rating is None and extra.rating is not None:
        primary.rating = extra.rating
    if primary.review_count is None and extra.review_count is not None:
        primary.review_count = extra.review_count
    if primary.latitude is None and extra.latitude is not None:
        primary.latitude = extra.latitude
    if primary.longitude is None and extra.longitude is not None:
        primary.longitude = extra.longitude
    if not primary.categories and extra.categories:
        primary.categories = list(extra.categories)
    if not primary.opening_hours and extra.opening_hours:
        primary.opening_hours = dict(extra.opening_hours)
    if primary.open_now is None and extra.open_now is not None:
        primary.open_now = extra.open_now
    if primary.price_level is None and extra.price_level is not None:
        primary.price_level = extra.price_level
    if primary.claimed is None and extra.claimed is not None:
        primary.claimed = extra.claimed
    if not primary.maps_url and extra.maps_url:
        primary.maps_url = extra.maps_url
    if not primary.place_id and extra.place_id:
        primary.place_id = extra.place_id
    if not primary.region and extra.region:
        primary.region = extra.region
    if len(extra.reviews) > len(primary.reviews):
        primary.reviews = list(extra.reviews)
    primary.emails = _union(primary.emails, extra.emails)
    primary.enriched = primary.enriched or extra.enriched


def _union(left: list[str], right: list[str]) -> list[str]:
    seen = {item.lower() for item in left}
    merged = list(left)
    for item in right:
        if item.lower() in seen:
            continue
        seen.add(item.lower())
        merged.append(item)
    return merged


def dedupe_businesses(businesses: list[Business]) -> list[Business]:
    """Collapse repeats. Different place ids are different businesses."""
    by_id: dict[str, Business] = {}
    order: list[Business] = []
    without_id: list[Business] = []

    for business in businesses:
        place_id = (business.place_id or "").strip()
        if place_id:
            existing = by_id.get(place_id)
            if existing is None:
                by_id[place_id] = business
                order.append(business)
            else:
                _fill(existing, business)
        else:
            without_id.append(business)

    coord_index = {}
    for business in order:
        key = _coord_key(business)
        if key is not None and key not in coord_index:
            coord_index[key] = business

    loose: dict[tuple[object, ...], Business] = {}
    for business in without_id:
        key = _coord_key(business)
        if key is not None and key in coord_index:
            _fill(coord_index[key], business)
            continue
        identity: tuple[object, ...]
        if key is not None:
            identity = ("coord", key)
        else:
            identity = ("name", _norm_name(business.name), _norm_name(business.address))
        existing = loose.get(identity)
        if existing is None:
            loose[identity] = business
            order.append(business)
        else:
            _fill(existing, business)
    return order


def fill_missing(primary: Business, extra: Business) -> Business:
    """Copy empty fields from ``extra`` onto ``primary`` and return ``primary``."""
    _fill(primary, extra)
    return primary


def merge_reviews(primary: list[Review], extra: list[Review]) -> list[Review]:
    return dedupe_review_lists(primary, extra)


def dedupe_review_lists(*groups: list[Review]) -> list[Review]:
    merged: list[Review] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for review in group:
            key = (review.author.strip().lower(), review.text.strip())
            if key in seen:
                continue
            seen.add(key)
            merged.append(review)
    return merged
