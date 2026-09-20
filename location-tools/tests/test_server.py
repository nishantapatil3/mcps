"""Tool-level behaviour: source labelling, accuracy disclosure, error shapes."""

from __future__ import annotations

import httpx
import pytest
from payloads import GEOJS_OK, IPINFO_OK, IPWHO_OK, NOMINATIM_PLACE, OPEN_METEO_TZ

from location_tools import config, server


@pytest.fixture
def ip_routes(route):
    """All upstreams answering successfully."""
    route(
        {
            "ipwho.is": IPWHO_OK,
            "ipinfo.io": IPINFO_OK,
            "geojs.io": GEOJS_OK,
            "open-meteo": OPEN_METEO_TZ,
            "/reverse": NOMINATIM_PLACE,
            "/search": [NOMINATIM_PLACE],
        }
    )


@pytest.fixture
def configured_home(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        config.Settings(home=(48.8584, 2.2945), home_label="Home, Paris"),
    )


# --- current_location ---


def test_current_location_falls_back_to_ip_and_says_so(ip_routes):
    result = server.current_location()

    assert result["source"] == "ip_geolocation"
    # The caveat has to survive into the payload; a bare coordinate would invite
    # the model to present a guess as fact.
    assert "accuracy_note" in result
    assert result["agreement"] == "close"
    assert result["timezone_info"]["timezone"] == "Asia/Tokyo"


def test_configured_home_wins_over_ip(configured_home, route):
    # No IP providers routed at all: if the tool tried one, the request would fail.
    route({"open-meteo": OPEN_METEO_TZ})
    result = server.current_location()

    assert result["source"] == "configured"
    assert result["latitude"] == 48.8584
    assert result["label"] == "Home, Paris"
    assert "not an IP estimate" in result["accuracy_note"]
    assert "providers" not in result


def test_current_location_reports_provider_failures(route):
    route(
        {
            "ipwho.is": IPWHO_OK,
            "ipinfo.io": httpx.Response(503, text="down"),
            "geojs.io": httpx.Response(500, text="down"),
            "open-meteo": OPEN_METEO_TZ,
        }
    )
    result = server.current_location()

    assert result["agreement"] == "single-provider"
    assert any("ipinfo.io" in w for w in result["warnings"])


def test_current_location_degrades_when_timezone_fails(route):
    route(
        {"ipwho.is": IPWHO_OK, "ipinfo.io": IPINFO_OK, "geojs.io": GEOJS_OK},
        default_status=503,  # open-meteo unrouted
    )
    result = server.current_location()

    # The position still comes back; only the timezone is missing.
    assert result["latitude"] == pytest.approx(35.02, abs=0.01)
    assert any("timezone" in w for w in result["warnings"])


def test_current_location_error_suggests_configuring_home(route):
    route({}, default_status=503)
    result = server.current_location()

    assert "error" in result
    assert "LOCATION_TOOLS_HOME" in result["error"]


def test_include_address_adds_a_reverse_geocode(ip_routes):
    result = server.current_location(include_address=True)
    assert result["address"]["address"]["city"] == "Kyoto"


# --- location_details ---


def test_location_details_accepts_a_place_name(ip_routes):
    result = server.location_details("Kyoto")
    assert result["source"] == "query"
    assert result["place"]["address"]["city"] == "Kyoto"
    assert result["timezone_info"]["timezone"] == "Asia/Tokyo"


def test_location_details_accepts_raw_coordinates(ip_routes):
    result = server.location_details("35.0210,135.7538")
    assert result["source"] == "coordinates"
    assert result["latitude"] == pytest.approx(35.0210)


def test_location_details_defaults_to_current_location(ip_routes):
    result = server.location_details()
    assert result["source"] == "ip_geolocation"
    # The IP caveat must propagate through this tool too, not just current_location.
    assert "accuracy_note" in result


def test_location_details_can_skip_timezone(ip_routes):
    result = server.location_details("Kyoto", include_timezone=False)
    assert "timezone_info" not in result


def test_location_details_reports_unresolvable_query(route):
    route({"/search": []})
    result = server.location_details("Definitely Not A Place")
    assert "error" in result


# --- geocode / reverse_geocode ---


def test_geocode_returns_ranked_candidates(route):
    route({"/search": [NOMINATIM_PLACE, {**NOMINATIM_PLACE, "lat": "35.5", "lon": "135.9"}]})
    result = server.geocode_tool("Kyoto")
    assert result["count"] == 2
    assert result["results"][0]["short_label"] == "Kyoto, Kyoto Prefecture, Japan"


def test_geocode_no_match_carries_a_hint(route):
    route({"/search": []})
    result = server.geocode_tool("Asdfghjkl")
    assert result["count"] == 0
    assert "note" in result


def test_geocode_empty_query_is_an_error(route):
    assert "error" in server.geocode_tool("  ")


def test_reverse_geocode_tool_returns_place(route):
    route({"/reverse": NOMINATIM_PLACE})
    result = server.reverse_geocode_tool(35.0210, 135.7538)
    assert result["place"]["address"]["state"] == "Kyoto Prefecture"


def test_reverse_geocode_tool_rejects_bad_latitude(route):
    result = server.reverse_geocode_tool(95.0, 0.0)
    assert "error" in result
    assert "latitude" in result["error"]


# --- ip_location ---


def test_ip_location_looks_up_an_explicit_address(ip_routes):
    result = server.ip_location("203.0.113.7")
    assert result["query_ip"] == "203.0.113.7"
    assert result["agreement"] == "close"


def test_ip_location_rejects_empty_and_urls(route):
    assert "error" in server.ip_location("")
    assert "error" in server.ip_location("https://example.com/path")
    assert "error" in server.ip_location("1.2.3.4 5.6.7.8")


# --- location_timezone ---


def test_timezone_for_a_named_place(ip_routes):
    result = server.location_timezone("Kyoto")
    assert result["timezone"] == "Asia/Tokyo"
    assert result["utc_offset"] == "+09:00"


def test_timezone_accepts_explicit_coordinates(route):
    route({"open-meteo": OPEN_METEO_TZ})
    result = server.location_timezone(latitude=35.02, longitude=135.75)
    assert result["timezone"] == "Asia/Tokyo"


def test_timezone_defaults_to_current_location(ip_routes):
    result = server.location_timezone()
    assert result["timezone"] == "Asia/Tokyo"
    assert result["source"] == "ip_geolocation"


# --- distance ---


def test_distance_between_two_named_places(route_fn):
    def responder(request: httpx.Request):
        # Vary the reply by query so the two legs resolve to different points.
        if "Osaka" in request.url.params.get("q", ""):
            return [{**NOMINATIM_PLACE, "lat": "34.6937", "lon": "135.5023"}]
        return [NOMINATIM_PLACE]

    route_fn(responder)

    result = server.distance_tool(to="Osaka", origin="Kyoto")
    # Kyoto to Osaka is roughly 40 km apart.
    assert result["distance_km"] == pytest.approx(40, abs=10)
    assert result["unit"] == "km"
    assert result["from"]["label"]
    assert result["to"]["label"]


def test_distance_accepts_coordinate_strings(route):
    result = server.distance_tool(to="0,10", origin="0,0")
    assert result["distance_km"] == pytest.approx(1111.95, abs=0.1)
    assert result["initial_bearing_compass"] == "E"


def test_distance_from_current_location_flags_inherited_error(ip_routes):
    result = server.distance_tool(to="0,0")
    assert any("IP-estimated" in note for note in result["notes"])


def test_distance_from_configured_home_has_no_accuracy_note(configured_home, route):
    route({})
    result = server.distance_tool(to="48.8584,2.2945")
    assert result["distance_km"] == pytest.approx(0, abs=0.001)
    assert "notes" not in result


def test_distance_rejects_bad_unit(route):
    result = server.distance_tool(to="0,0", origin="1,1", unit="furlongs")
    assert "error" in result


def test_distance_requires_a_destination(route):
    assert "error" in server.distance_tool(origin="0,0")


@pytest.mark.parametrize("unit,expected", [("km", 1111.95), ("mi", 690.95), ("nmi", 600.4)])
def test_distance_units(route, unit, expected):
    result = server.distance_tool(to="0,10", origin="0,0", unit=unit)
    assert result["distance"] == pytest.approx(expected, rel=0.001)
