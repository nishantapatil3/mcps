"""Geocoding, reverse geocoding, and timezone resolution."""

from __future__ import annotations

import httpx
import pytest
from payloads import NOMINATIM_PLACE, OPEN_METEO_TZ

from location_tools import config, places
from location_tools.geo import CoordinateError
from location_tools.http_client import UpstreamError


# --- Forward geocoding ---


def test_geocode_normalizes_a_result(route):
    route({"/search": [NOMINATIM_PLACE]})
    (match,) = places.geocode("Kyoto")

    assert match["latitude"] == pytest.approx(35.0210)
    assert match["longitude"] == pytest.approx(135.7538)
    assert match["short_label"] == "Kyoto, Kyoto Prefecture, Japan"
    assert match["address"]["country_code"] == "jp"


def test_geocode_names_bounding_box_edges(route):
    # Nominatim returns [south, north, west, east] as strings; unnamed, the
    # caller has to guess the order.
    route({"/search": [NOMINATIM_PLACE]})
    (match,) = places.geocode("Kyoto")
    assert match["bounding_box"] == {
        "south": 34.8747,
        "north": 35.3175,
        "west": 135.5647,
        "east": 135.8679,
    }


def test_geocode_empty_result_is_an_empty_list(route):
    route({"/search": []})
    assert places.geocode("Nowhere At All") == []


def test_geocode_rejects_blank_query(route):
    with pytest.raises(ValueError):
        places.geocode("   ")


def test_geocode_limit_is_clamped(capture):
    seen = capture()
    places.geocode("Kyoto", limit=999)
    assert seen["limit"] == str(places.MAX_RESULTS)


def test_geocode_skips_unparseable_entries_without_failing(route):
    route({"/search": [{"display_name": "no coords here"}, NOMINATIM_PLACE]})
    matches = places.geocode("Kyoto")
    assert len(matches) == 1


def test_geocode_rejects_unexpected_shape(route):
    route({"/search": {"unexpected": "object"}})
    with pytest.raises(UpstreamError):
        places.geocode("Kyoto")


def test_resolve_place_raises_when_nothing_matches(route):
    route({"/search": []})
    with pytest.raises(UpstreamError):
        places.resolve_place("Definitely Not A Place")


def test_country_codes_are_forwarded_lowercased(capture):
    seen = capture()
    places.geocode("Cambridge", country_codes=" GB,IE ")
    assert seen["countrycodes"] == "gb,ie"


# --- Reverse geocoding ---


def test_reverse_geocode_returns_a_place(route):
    route({"/reverse": NOMINATIM_PLACE})
    place = places.reverse_geocode(35.0210, 135.7538)
    assert place["address"]["city"] == "Kyoto"


def test_reverse_geocode_surfaces_nominatim_error(route):
    # Open ocean legitimately has no address.
    route({"/reverse": {"error": "Unable to geocode"}})
    with pytest.raises(UpstreamError):
        places.reverse_geocode(0.0, 0.0)


def test_reverse_geocode_validates_coordinates(route):
    with pytest.raises(CoordinateError):
        places.reverse_geocode(95.0, 0.0)


def test_reverse_geocode_clamps_zoom(capture):
    seen = capture(NOMINATIM_PLACE)
    places.reverse_geocode(35.0, 135.0, zoom=99)
    assert seen["zoom"] == "18"


# --- Timezone ---


def test_timezone_resolves_offset_and_local_time(route):
    route({"open-meteo": OPEN_METEO_TZ})
    info = places.timezone_at(35.02, 135.75)

    assert info["timezone"] == "Asia/Tokyo"
    assert info["utc_offset"] == "+09:00"
    assert info["utc_offset_seconds"] == 32400
    assert info["local_time"].endswith("+09:00")


def test_timezone_formats_negative_offsets(route):
    route({"open-meteo": {**OPEN_METEO_TZ, "utc_offset_seconds": -28800}})
    info = places.timezone_at(37.77, -122.42)
    assert info["utc_offset"] == "-08:00"


def test_timezone_formats_half_hour_offsets(route):
    # India is UTC+05:30; a naive hours-only formatter gets this wrong.
    route({"open-meteo": {**OPEN_METEO_TZ, "utc_offset_seconds": 19800}})
    info = places.timezone_at(28.61, 77.21)
    assert info["utc_offset"] == "+05:30"


def test_timezone_missing_field_is_an_error(route):
    route({"open-meteo": {"latitude": 0, "longitude": 0}})
    with pytest.raises(UpstreamError):
        places.timezone_at(0, 0)


# --- Configuration plumbing ---


def test_accept_language_is_sent_on_search(capture):
    # Without this, Nominatim returns names in the local language of whatever was
    # queried ("日本" instead of "Japan"), so output language would track the query.
    seen = capture()
    places.geocode("Kyoto")
    assert seen["accept-language"] == "en"


def test_accept_language_is_sent_on_reverse(capture):
    seen = capture(NOMINATIM_PLACE)
    places.reverse_geocode(35.0, 135.0)
    assert seen["accept-language"] == "en"


def test_accept_language_is_configurable(capture, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(accept_language="ja"))
    seen = capture()
    places.geocode("Kyoto")
    assert seen["accept-language"] == "ja"


def test_self_hosted_nominatim_url_is_honoured(capture, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(nominatim_url="https://geo.internal"))
    seen = capture()
    places.geocode("Kyoto")
    assert seen["__url__"].startswith("https://geo.internal/search")
