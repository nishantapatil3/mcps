"""IP geolocation: per-provider parsing and cross-provider consensus."""

from __future__ import annotations

import httpx
import pytest

from location_tools.http_client import UpstreamError
from location_tools.ipgeo import IPLocation, locate_ip, summarize

from payloads import GEOJS_OK, IPINFO_OK, IPWHO_OK


def _loc(provider: str, lat: float, lon: float, **kwargs) -> IPLocation:
    return IPLocation(provider=provider, latitude=lat, longitude=lon, **kwargs)


# --- Parsing ---


def test_all_three_providers_parse(route):
    route({"ipwho.is": IPWHO_OK, "ipinfo.io": IPINFO_OK, "geojs.io": GEOJS_OK})
    summary, warnings = locate_ip("203.0.113.7")

    assert warnings == []
    assert {p["provider"] for p in summary["providers"]} == {
        "ipwho.is",
        "ipinfo.io",
        "geojs.io",
    }
    assert summary["city"] == "Kyoto"
    assert summary["timezone"] == "Asia/Tokyo"


def test_geojs_string_coordinates_are_coerced(route):
    # geojs returns lat/lon as strings while the others use numbers.
    route({"geojs.io": GEOJS_OK}, default_status=500)
    summary, _ = locate_ip("203.0.113.7")
    assert summary["latitude"] == pytest.approx(35.019, abs=0.001)
    assert isinstance(summary["latitude"], float)


def test_ipwho_body_level_failure_is_detected(route):
    # ipwho.is reports "Invalid IP address" with HTTP 200, so status is not enough.
    route(
        {
            "ipwho.is": {"ip": "999.1.1.1", "success": False, "message": "Invalid IP address"},
            "ipinfo.io": IPINFO_OK,
        },
        default_status=500,
    )
    summary, warnings = locate_ip("999.1.1.1")

    assert any("Invalid IP address" in w for w in warnings)
    assert [p["provider"] for p in summary["providers"]] == ["ipinfo.io"]


def test_ipinfo_missing_loc_field_is_an_error(route):
    route({"ipinfo.io": {"ip": "203.0.113.7", "city": "Nowhere"}}, default_status=500)
    with pytest.raises(UpstreamError):
        locate_ip("203.0.113.7")


# --- Failure handling ---


def test_one_dead_provider_does_not_sink_the_others(route):
    route(
        {
            "ipwho.is": httpx.Response(503, text="upstream down"),
            "ipinfo.io": IPINFO_OK,
            "geojs.io": GEOJS_OK,
        }
    )
    summary, warnings = locate_ip("203.0.113.7")

    assert len(summary["providers"]) == 2
    assert any("ipwho.is" in w for w in warnings)


def test_all_providers_failing_raises(route):
    route({}, default_status=503)
    with pytest.raises(UpstreamError):
        locate_ip("203.0.113.7")


def test_malformed_json_is_reported_not_raised(route):
    route(
        {
            "ipwho.is": httpx.Response(200, text="<html>not json</html>"),
            "ipinfo.io": IPINFO_OK,
        },
        default_status=500,
    )
    summary, warnings = locate_ip("203.0.113.7")
    assert len(summary["providers"]) == 1
    assert any("ipwho.is" in w for w in warnings)


def test_rate_limit_is_named_in_the_warning(route):
    route(
        {"ipwho.is": httpx.Response(429, text="slow down"), "ipinfo.io": IPINFO_OK},
        default_status=500,
    )
    _, warnings = locate_ip("203.0.113.7")
    assert any("429" in w for w in warnings)


# --- Consensus ---


def test_close_agreement_when_providers_converge():
    summary = summarize(
        [
            _loc("a", 35.0211, 135.7538, city="Kyoto"),
            _loc("b", 35.0250, 135.7600),
            _loc("c", 35.0190, 135.7500),
        ]
    )
    assert summary["agreement"] == "close"
    assert summary["provider_spread_km"] < 25
    assert "agree" in summary["accuracy_note"]


def test_poor_agreement_when_providers_are_continents_apart():
    summary = summarize(
        [
            _loc("a", 35.0, 135.0),
            _loc("b", 51.5, -0.1),
            _loc("c", -33.9, 151.2),
        ]
    )
    assert summary["agreement"] == "poor"
    assert "unreliable" in summary["accuracy_note"]


def test_loose_agreement_for_a_metro_scale_disagreement():
    # ~70 km apart: the real spread observed between providers for one
    # residential IP during development.
    summary = summarize([_loc("a", 37.3394, -121.8950), _loc("b", 37.9480, -122.0608)])
    assert summary["agreement"] == "loose"


def test_single_provider_is_labelled_as_uncross_checked():
    summary = summarize([_loc("only", 35.0, 135.0)])
    assert summary["agreement"] == "single-provider"
    assert "no cross-check" in summary["accuracy_note"]
    assert summary["provider_spread_km"] == 0.0


def test_median_rejects_a_country_centroid_outlier():
    # A provider with no data for a prefix returns the country's centre. The
    # median must discard it; a mean would be dragged toward it.
    summary = summarize(
        [
            _loc("good1", 37.3394, -121.8950),
            _loc("good2", 37.3400, -121.8900),
            _loc("centroid", 37.7510, -97.8220),  # geographic centre of the USA
        ]
    )
    assert summary["latitude"] == pytest.approx(37.34, abs=0.01)
    assert summary["longitude"] == pytest.approx(-121.89, abs=0.01)


def test_country_level_accuracy_is_flagged():
    summary = summarize(
        [
            _loc("precise", 37.3394, -121.8950, accuracy_radius_km=5),
            _loc("vague", 37.7510, -97.8220, accuracy_radius_km=1000),
        ]
    )
    assert "country-level" in summary["accuracy_note"]
    assert "vague" in summary["accuracy_note"]


def test_descriptive_fields_come_from_the_provider_nearest_consensus():
    # The outlier's city name must not win, or the label would contradict the
    # coordinates reported beside it.
    summary = summarize(
        [
            _loc("good1", 37.3394, -121.8950, city="San Jose"),
            _loc("good2", 37.3400, -121.8900, city="San Jose"),
            _loc("outlier", 40.0, -100.0, city="Wichita"),
        ]
    )
    assert summary["city"] == "San Jose"


def test_empty_input_raises():
    with pytest.raises(UpstreamError):
        summarize([])
