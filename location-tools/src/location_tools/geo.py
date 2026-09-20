"""Coordinate parsing, validation, and great-circle geometry.

Pure functions with no I/O, so the numeric behaviour is testable without a
network. Distances use the haversine formula on a spherical earth: it is stable
at every scale including antipodal pairs (unlike the naive spherical law of
cosines, which loses precision at short range), and its ~0.5% worst-case error
against the WGS-84 ellipsoid is far below the accuracy of any input this server
can obtain.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Equatorial/polar mean radius used by the haversine formula (IUGG mean radius).
EARTH_RADIUS_KM = 6371.0088

KM_PER_MILE = 1.609344
KM_PER_NAUTICAL_MILE = 1.852

# Compass points for a 16-wind rose: 360/16 = 22.5 degrees per sector.
_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)

UNITS = ("km", "mi", "nmi")

# "40.7128, -74.0060" and "40.7128 -74.0060" are both common; a bare hyphen-minus
# is the only sign accepted, since a Unicode minus would silently parse wrong.
_PAIR = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[,\s]\s*([+-]?\d+(?:\.\d+)?)\s*$")


class CoordinateError(ValueError):
    """Raised when a coordinate pair is malformed or out of range."""


def parse_coordinates(text: str) -> tuple[float, float]:
    """Parse 'lat,lon' or 'lat lon' into a validated (lat, lon) tuple."""
    match = _PAIR.match(text or "")
    if match is None:
        raise CoordinateError(
            f"could not parse coordinates from {text!r}; expected 'lat,lon' e.g. '37.77,-122.42'"
        )
    return validate_coordinates(float(match.group(1)), float(match.group(2)))


def validate_coordinates(lat: float, lon: float) -> tuple[float, float]:
    """Range-check a coordinate pair.

    Latitude beyond +-90 is impossible, so it is an error rather than something to
    normalize. Longitude is cyclic and is wrapped into [-180, 180]: +181 is an
    unambiguous synonym for -179, and rejecting it would fail requests that are
    merely unnormalized rather than wrong.
    """
    for name, value in (("latitude", lat), ("longitude", lon)):
        if not math.isfinite(value):
            raise CoordinateError(f"{name} must be a finite number, got {value!r}")
    if not -90.0 <= lat <= 90.0:
        raise CoordinateError(f"latitude {lat} is out of range (must be between -90 and 90)")
    if not -180.0 <= lon <= 180.0:
        # ((lon + 180) % 360) - 180 maps onto [-180, 180).
        lon = ((lon + 180.0) % 360.0) - 180.0
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    # asin(sqrt(a)) with a clamped to 1 guards against a float overshoot at
    # antipodal points, where rounding can push `a` a hair above 1.0.
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing in degrees clockwise from true north.

    This is the heading at the start of the path only. A great circle's heading
    changes continuously along the route, so it is not a constant compass course.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_point(bearing: float) -> str:
    """Nearest 16-wind compass point for a bearing in degrees."""
    return _COMPASS[int((bearing % 360.0) / 22.5 + 0.5) % 16]


def convert_km(km: float, unit: str) -> float:
    if unit == "km":
        return km
    if unit == "mi":
        return km / KM_PER_MILE
    if unit == "nmi":
        return km / KM_PER_NAUTICAL_MILE
    raise ValueError(f"unknown unit '{unit}'; use one of {UNITS}")


def describe_distance(
    lat1: float, lon1: float, lat2: float, lon2: float, unit: str = "km"
) -> dict[str, Any]:
    """Distance and initial bearing between two validated points."""
    km = haversine_km(lat1, lon1, lat2, lon2)
    bearing = initial_bearing(lat1, lon1, lat2, lon2)
    return {
        "distance": round(convert_km(km, unit), 3),
        "unit": unit,
        "distance_km": round(km, 3),
        "initial_bearing_deg": round(bearing, 1),
        "initial_bearing_compass": compass_point(bearing),
        "from": {"latitude": lat1, "longitude": lon1},
        "to": {"latitude": lat2, "longitude": lon2},
        "method": "haversine great-circle on a spherical earth (~0.5% worst-case error)",
    }
