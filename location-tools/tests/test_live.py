"""Live checks against the real upstream services.

Opt-in via `pytest -m network`. These exist because every offline test mocks the
transport, so a provider changing its response shape would pass the whole default
suite while breaking the server in practice. Assertions are deliberately loose:
they verify the contract this server depends on, not specific coordinates, which
legitimately drift as the databases are updated.
"""

from __future__ import annotations

import pytest

from location_tools import places, server
from location_tools.geo import validate_coordinates
from location_tools.ipgeo import _PROVIDERS, locate_ip

pytestmark = pytest.mark.network


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda p: p.name)
def test_each_provider_still_parses(provider):
    """Each provider individually still returns what its parser expects."""
    from location_tools.fetch import get_json

    location = provider.parser(get_json(provider.url_for("8.8.8.8"), use_cache=False))
    validate_coordinates(location.latitude, location.longitude)
    assert location.country_code


def test_locate_ip_reaches_consensus():
    summary, warnings = locate_ip("8.8.8.8")
    assert summary["agreement"] in ("close", "loose", "poor", "single-provider")
    assert summary["country_code"].upper() == "US"
    # At least two of the three providers should be reachable.
    assert len(summary["providers"]) >= 2, warnings


def test_geocode_finds_a_landmark():
    (match,) = places.geocode("Eiffel Tower", limit=1)
    assert match["latitude"] == pytest.approx(48.8584, abs=0.01)
    assert match["longitude"] == pytest.approx(2.2945, abs=0.01)
    assert match["address"]["country"] == "France"


def test_geocode_country_filter_applies():
    matches = places.geocode("Cambridge", limit=5, country_codes="gb")
    assert matches
    assert all(m["address"].get("country_code") == "gb" for m in matches)


def test_reverse_geocode_resolves_a_known_point():
    place = places.reverse_geocode(48.8584, 2.2945)
    assert place["address"]["country_code"] == "fr"


def test_timezone_resolves_a_known_zone():
    info = places.timezone_at(35.0211, 135.7538)
    assert info["timezone"] == "Asia/Tokyo"
    assert info["utc_offset_seconds"] == 32400


def test_timezone_handles_a_half_hour_zone():
    # India is UTC+05:30 year-round; catches hour-only offset formatting.
    info = places.timezone_at(28.6139, 77.2090)
    assert info["timezone"] == "Asia/Kolkata"
    assert info["utc_offset"] == "+05:30"


def test_current_location_end_to_end():
    result = server.current_location()
    assert "error" not in result
    assert result["source"] in ("configured", "ip_geolocation")
    # The accuracy disclosure must be present on a real call, not just in mocks.
    assert result["accuracy_note"]
    validate_coordinates(result["latitude"], result["longitude"])


def test_distance_between_real_places():
    result = server.distance_tool(to="Osaka, Japan", origin="Kyoto, Japan")
    # Published Kyoto-Osaka great-circle distance is ~40 km.
    assert result["distance_km"] == pytest.approx(40, abs=15)


def test_location_details_end_to_end():
    result = server.location_details("Kyoto, Japan")
    assert result["place"]["address"]["country"] == "Japan"
    assert result["timezone_info"]["timezone"] == "Asia/Tokyo"
