"""Business records returned by every backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Review:
    """One public review. At most five are kept on a business."""

    text: str
    author: str
    rating: float | None
    date: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "author": self.author,
            "rating": self.rating,
            "date": self.date,
        }


@dataclass
class Business:
    """A deduplicated business listing."""

    name: str
    address: str = ""
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    categories: list[str] = field(default_factory=list)
    opening_hours: dict[str, object] = field(default_factory=dict)
    open_now: bool | None = None
    price_level: str | int | None = None
    claimed: bool | None = None
    reviews: list[Review] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    place_id: str | None = None
    maps_url: str | None = None
    region: str = ""
    enriched: bool = False

    def to_dict(self) -> dict[str, object]:
        hours = dict(self.opening_hours)
        if "open_now" not in hours:
            hours["open_now"] = self.open_now
        return {
            "place_id": self.place_id,
            "name": self.name,
            "address": self.address,
            "phone": self.phone,
            "website": self.website,
            "rating": self.rating,
            "review_count": self.review_count,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "categories": list(self.categories),
            "opening_hours": hours,
            "open_now": self.open_now,
            "price_level": self.price_level,
            "claimed": self.claimed,
            "reviews": [review.to_dict() for review in self.reviews],
            "emails": list(self.emails),
            "maps_url": self.maps_url,
        }


@dataclass
class ScrapeResult:
    """Metadata plus the businesses saved for one search."""

    query: str
    location: str
    businesses: list[Business]
    backend: str
    timestamp: str = field(default_factory=utc_now)

    @property
    def total_found(self) -> int:
        return len(self.businesses)

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "query": self.query,
            "location": self.location,
            "total_found": self.total_found,
            "backend": self.backend,
            "businesses": [business.to_dict() for business in self.businesses],
        }
