"""Parser tests against the repository's saved Maps fixtures."""

import json
from pathlib import Path

from paths import FIXTURES, RAW_PLACE, REVIEW_DIR

from gmaps_service.parsers import (
    business_from_place_payload,
    business_from_places,
    extract_place_id,
    parse_place_html,
    parse_reviews,
    parse_reviews_html,
    parse_search_results,
)


def test_place_payload_matches_kipriakon_fixture():
    business = business_from_place_payload(RAW_PLACE.read_bytes())

    assert business.name == "Kipriakon"
    assert business.categories == ["Restaurant"]
    assert business.address == "Old port, Limassol 3042"
    assert business.place_id == "ChIJDdnwdv0y5xQRRytw1ihZQeU"
    assert business.rating == 4.2
    assert business.review_count == 396
    assert business.latitude == 34.670595399999996
    assert business.longitude == 33.042456699999995
    assert business.price_level == "€€"
    assert business.website is None
    assert business.claimed is True
    assert business.open_now is False
    assert business.region == "CY"
    assert business.phone == "+35725101555"
    monday = next(line for line in business.opening_hours["weekday_text"] if line.startswith("Monday"))
    assert "12:30" in monday
    assert business.opening_hours["open_now"] is False
    authors = [review.author for review in business.reviews]
    assert "Cristina Dragoi" in authors
    assert len(business.reviews) <= 5
    matched = next(review for review in business.reviews if review.author == "Cristina Dragoi")
    assert "AMAZING" in matched.text
    assert matched.rating == 5


def test_saved_review_fixtures():
    native = parse_reviews((REVIEW_DIR / "review_native_with_reply.json").read_text(encoding="utf-8"))
    assert len(native) == 1
    assert native[0].author == "E. Ö."
    assert native[0].rating == 1
    assert "enttäuscht" in native[0].text.lower()
    assert native[0].date.startswith("2026-02-27")

    aggregator = parse_reviews((REVIEW_DIR / "review_aggregator.json").read_text(encoding="utf-8"))
    assert aggregator[0].author == "Tripadvisor User"
    assert aggregator[0].rating == 8.5
    assert aggregator[0].text

    no_text = parse_reviews((REVIEW_DIR / "review_native_no_text.json").read_text(encoding="utf-8"))
    assert no_text[0].author == "Lysann Lieblang"
    assert no_text[0].rating == 5
    assert no_text[0].text == ""

    untranslated = parse_reviews((REVIEW_DIR / "review_native_no_translation.json").read_text(encoding="utf-8"))
    assert untranslated[0].author == "Olena Kuzhel"
    assert untranslated[0].text.startswith("I had been recently")


def test_search_results_fixture_unwraps_website_and_review():
    found = parse_search_results((FIXTURES / "search_results.json").read_text(encoding="utf-8"))
    assert len(found) == 1
    business = found[0]
    assert business.name == "Blue Bottle Coffee"
    assert business.website == "https://bluebottlecoffee.com/"
    assert business.place_id == "ChIJbluebottleexample"
    assert business.rating == 4.6
    assert business.review_count == 842
    assert business.latitude == 40.71821
    assert business.longitude == -73.96312
    assert business.phone == "+12123439000"
    assert business.open_now is True
    assert business.categories == ["Coffee shop", "Cafe"]
    assert business.address == "76 N 4th St, Brooklyn, NY 11249"
    assert business.reviews[0].author == "Sam Lee"
    assert business.reviews[0].text == "Excellent espresso."
    assert "Monday" in business.opening_hours["weekday_text"][0]


def test_nested_place_array_is_found():
    payload = json.loads((FIXTURES / "search_results.json").read_text(encoding="utf-8"))
    darray = payload[0][1][1][14]
    business = business_from_place_payload(json.dumps({"state": [None, darray]}))
    assert business.name == "Blue Bottle Coffee"
    assert business.place_id == "ChIJbluebottleexample"


def test_xssi_prefix_is_accepted():
    payload = (FIXTURES / "search_results.json").read_text(encoding="utf-8")
    found = parse_search_results(")]}'\n" + payload)
    assert found[0].name == "Blue Bottle Coffee"


def test_place_panel_dom_fixture():
    html = (FIXTURES / "place_panel.html").read_text(encoding="utf-8")
    url = "https://www.google.com/maps/place/Blue+Bottle/@40.71821,-73.96312,17z?place_id=ChIJpanel"
    business = parse_place_html(html, url)
    assert business.name == "Blue Bottle Coffee"
    assert business.address == "76 N 4th St, Brooklyn, NY 11249"
    assert business.phone == "+12123439000"
    assert business.website == "https://bluebottlecoffee.com/cafes"
    assert business.rating == 4.6
    assert business.review_count == 1204
    assert business.categories == ["Coffee shop"]
    assert business.open_now is True
    assert business.claimed is False
    assert business.price_level == "$$"
    assert business.latitude == 40.71821
    assert business.longitude == -73.96312
    assert business.place_id == "ChIJpanel"
    assert business.opening_hours["periods"][0]["day"] == "Monday"
    reviews = parse_reviews_html(html)
    assert reviews[0].author == "Sam Lee"
    assert reviews[0].rating == 5
    assert reviews[0].text == "Excellent espresso."


def test_places_api_fixture():
    payload = json.loads((FIXTURES / "places_search.json").read_text(encoding="utf-8"))
    official = business_from_places(payload["places"][0])
    assert official.name == "Blue Bottle Coffee"
    assert official.phone == "+12123439000"
    assert official.website == "https://bluebottlecoffee.com/cafes"
    assert official.price_level == 2
    assert official.open_now is True
    assert official.claimed is None
    assert "Coffee Shop" in official.categories
    assert "Point Of Interest" not in official.categories
    assert official.opening_hours["periods"][0] == {"day": "Monday", "open": "07:00", "close": "19:00"}
    assert [review.author for review in official.reviews] == ["Sam Lee", "Ada"]
    assert official.reviews[0].date == "2026-10-20T15:00:00Z"

    social = business_from_places(payload["places"][1])
    assert social.website is None


def test_extract_place_id_prefers_canonical_id():
    assert extract_place_id(
        "https://www.google.com/maps/place/Joe's+Pizza/@40.75,-73.98,17z?place_id=ChIJexample&hl=en"
    ) == "ChIJexample"
    assert extract_place_id(
        "https://www.google.com/maps/place/Coffee/data=!4m7!3m6!1s0x89c2599b5a24d7fd:0x9e354f6cf514b9fc"
        "!19sChIJ_dckWptZwokR_LkU9WxPNZ4"
    ) == "ChIJ_dckWptZwokR_LkU9WxPNZ4"
    assert extract_place_id("https://www.google.com/maps/search/pizza+in+Brooklyn") is None
    assert extract_place_id("") is None


def test_place_panel_file_exists():
    assert Path(FIXTURES / "place_panel.html").is_file()
    assert RAW_PLACE.is_file()
