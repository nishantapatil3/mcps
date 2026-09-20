"""Coordinate parsing, validation, and great-circle geometry."""

from __future__ import annotations

import math

import pytest

from location_tools.geo import (
    CoordinateError,
    compass_point,
    convert_km,
    describe_distance,
    haversine_km,
    initial_bearing,
    parse_coordinates,
    validate_coordinates,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("40.7128,-74.0060", (40.7128, -74.0060)),
        ("40.7128, -74.0060", (40.7128, -74.0060)),
        ("40.7128 -74.0060", (40.7128, -74.0060)),
        ("  0,0  ", (0.0, 0.0)),
        ("-90,180", (-90.0, 180.0)),
        ("+40.5,+2.5", (40.5, 2.5)),
    ],
)
def test_parse_coordinates_accepts_common_forms(text, expected):
    assert parse_coordinates(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abc",
        "40.7",  # missing longitude
        "91,0",  # latitude out of range
        "40.7,-74.0,5",  # three components
        "nan,0",
        "1e5,0",  # scientific notation is not accepted, and would be out of range
        "40,7 -74,0",  # European decimal commas are ambiguous with the separator
    ],
)
def test_parse_coordinates_rejects_bad_input(text):
    with pytest.raises(CoordinateError):
        parse_coordinates(text)


def test_latitude_out_of_range_is_an_error():
    # Latitude has no cyclic interpretation, so it must not be silently wrapped.
    with pytest.raises(CoordinateError):
        validate_coordinates(90.5, 0)


@pytest.mark.parametrize(
    "lon,expected",
    [(181, -179.0), (-181, 179.0), (540, -180.0), (360, 0.0)],
)
def test_longitude_wraps_into_range(lon, expected):
    # Longitude is cyclic: +181 unambiguously means -179, so wrapping is correct
    # where rejecting would fail merely-unnormalized input.
    assert validate_coordinates(0, lon) == (0, expected)


def test_non_finite_coordinates_rejected():
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(CoordinateError):
            validate_coordinates(bad, 0)
        with pytest.raises(CoordinateError):
            validate_coordinates(0, bad)


@pytest.mark.parametrize(
    "name,a,b,c,d,expected_km,tolerance",
    [
        # Published great-circle distances between airport reference points.
        ("JFK-LHR", 40.6413, -73.7781, 51.4700, -0.4543, 5540, 0.02),
        ("SFO-NRT", 37.6213, -122.3790, 35.7720, 140.3929, 8280, 0.02),
        # Antipodal: half the circumference. The naive law-of-cosines formula
        # loses precision here, which is why haversine is used.
        ("antipodal", 0, 0, 0, 180, 20015, 0.01),
    ],
)
def test_haversine_matches_known_distances(name, a, b, c, d, expected_km, tolerance):
    assert haversine_km(a, b, c, d) == pytest.approx(expected_km, rel=tolerance)


def test_identical_points_are_zero_distance():
    assert haversine_km(45.0, 45.0, 45.0, 45.0) == 0.0


def test_distance_is_symmetric():
    forward = haversine_km(10, 20, -30, 140)
    backward = haversine_km(-30, 140, 10, 20)
    assert forward == pytest.approx(backward)


def test_short_distance_precision():
    # One degree of latitude is ~111 km everywhere; a tenth of that is ~11.1 km.
    assert haversine_km(0, 0, 0.1, 0) == pytest.approx(11.12, abs=0.05)


@pytest.mark.parametrize(
    "a,b,c,d,expected_deg,expected_point",
    [
        (0, 0, 10, 0, 0.0, "N"),
        (0, 0, 0, 10, 90.0, "E"),
        (0, 0, -10, 0, 180.0, "S"),
        (0, 0, 0, -10, 270.0, "W"),
    ],
)
def test_initial_bearing_cardinals(a, b, c, d, expected_deg, expected_point):
    bearing = initial_bearing(a, b, c, d)
    assert bearing == pytest.approx(expected_deg, abs=0.01)
    assert compass_point(bearing) == expected_point


@pytest.mark.parametrize(
    "bearing,point",
    [(0, "N"), (22.5, "NNE"), (45, "NE"), (180, "S"), (359, "N"), (360, "N"), (-1, "N")],
)
def test_compass_point_boundaries(bearing, point):
    assert compass_point(bearing) == point


def test_unit_conversion_against_definitions():
    assert convert_km(1.609344, "mi") == pytest.approx(1.0)
    assert convert_km(1.852, "nmi") == pytest.approx(1.0)
    assert convert_km(42.0, "km") == 42.0


def test_unknown_unit_rejected():
    with pytest.raises(ValueError):
        convert_km(1.0, "furlongs")


def test_describe_distance_reports_both_units_and_bearing():
    result = describe_distance(0, 0, 0, 10, unit="mi")
    assert result["unit"] == "mi"
    # 10 degrees of longitude at the equator: 10 * (2*pi*R/360) = ~1111.95 km.
    assert result["distance_km"] == pytest.approx(1111.95, abs=0.1)
    assert result["distance"] == pytest.approx(result["distance_km"] / 1.609344, abs=0.01)
    assert result["initial_bearing_compass"] == "E"
    assert result["from"] == {"latitude": 0, "longitude": 0}
