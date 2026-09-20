"""Geocoding and timezone lookup against keyless community services.

Forward and reverse geocoding use Nominatim (OpenStreetMap). Its usage policy is
binding: identify the application in the User-Agent, stay under 1 request/second,
and cache. `config` supplies the UA and `fetch` enforces the throttle and cache.

Timezone comes from Open-Meteo, whose forecast endpoint resolves an IANA zone for
a coordinate pair with `timezone=auto`. Using a weather API for this looks odd,
but the alternatives either need a key or ship a tzdata shapefile; this is one
keyless HTTPS call and returns the UTC offset already resolved for the date,
which is what callers actually need.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from . import config
from .fetch import get_json
from .geo import validate_coordinates
from .http_client import UpstreamError

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Nominatim's `zoom` for reverse geocoding: 18 is building level, 10 is city.
# 18 is the default because a caller asking to reverse a coordinate wants the
# most specific address available, and the response still carries every coarser
# component alongside it.
DEFAULT_REVERSE_ZOOM = 18

MAX_RESULTS = 10

# Nominatim address components ordered from most to least specific. Used to
# derive a short human label, since `display_name` is often absurdly long
# ("267, North Market Street, Saint James Square Historic District, ...").
_LOCALITY_KEYS = ("city", "town", "village", "municipality", "hamlet", "suburb", "county")


def _clean(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _short_label(address: dict[str, Any], display_name: str) -> str:
    """Build a 'City, State, Country' style label from address components."""
    locality = next((_clean(address.get(key)) for key in _LOCALITY_KEYS if address.get(key)), "")
    parts = [locality, _clean(address.get("state")), _clean(address.get("country"))]
    label = ", ".join(part for part in parts if part)
    return label or display_name


def _place(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Nominatim place into a flat, predictable shape."""
    address = raw.get("address") or {}
    lat, lon = validate_coordinates(float(raw["lat"]), float(raw["lon"]))
    payload: dict[str, Any] = {
        "name": _clean(raw.get("name")) or _short_label(address, _clean(raw.get("display_name"))),
        "display_name": _clean(raw.get("display_name")),
        "short_label": _short_label(address, _clean(raw.get("display_name"))),
        "latitude": lat,
        "longitude": lon,
        "category": _clean(raw.get("category") or raw.get("class")),
        "type": _clean(raw.get("type")),
        "place_rank": raw.get("place_rank"),
        "osm_type": _clean(raw.get("osm_type")),
        "osm_id": raw.get("osm_id"),
        "address": {k: v for k, v in address.items() if _clean(v)},
    }
    if raw.get("boundingbox"):
        # Nominatim orders this [south, north, west, east] as strings; naming the
        # edges prevents the caller guessing wrong.
        try:
            south, north, west, east = (float(v) for v in raw["boundingbox"])
            payload["bounding_box"] = {
                "south": south,
                "north": north,
                "west": west,
                "east": east,
            }
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in payload.items() if v not in ("", None, {})}


def geocode(query: str, limit: int = 5, country_codes: str = "") -> list[dict[str, Any]]:
    """Resolve a place name or address to candidate coordinates."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), MAX_RESULTS))

    params: dict[str, Any] = {
        "q": query,
        "format": "jsonv2",
        "limit": limit,
        "addressdetails": 1,
        "accept-language": config.settings.accept_language,
    }
    if country_codes.strip():
        params["countrycodes"] = country_codes.strip().lower()

    payload = get_json(f"{config.settings.nominatim_url}/search", params)
    if not isinstance(payload, list):
        raise UpstreamError("Nominatim search returned an unexpected response shape")

    places: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict) or "lat" not in raw or "lon" not in raw:
            continue
        try:
            places.append(_place(raw))
        except (TypeError, ValueError):
            continue  # skip an unparseable entry rather than failing the batch
    return places


def reverse_geocode(lat: float, lon: float, zoom: int = DEFAULT_REVERSE_ZOOM) -> dict[str, Any]:
    """Resolve coordinates to the nearest address."""
    lat, lon = validate_coordinates(lat, lon)
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": max(0, min(int(zoom), 18)),
        "addressdetails": 1,
        "accept-language": config.settings.accept_language,
    }
    payload = get_json(f"{config.settings.nominatim_url}/reverse", params)
    if not isinstance(payload, dict):
        raise UpstreamError("Nominatim reverse returned an unexpected response shape")
    if payload.get("error"):
        # Open ocean and Antarctica legitimately have no address.
        raise UpstreamError(f"Nominatim could not reverse geocode {lat},{lon}: {payload['error']}")
    if "lat" not in payload or "lon" not in payload:
        raise UpstreamError(f"Nominatim returned no address for {lat},{lon}")
    return _place(payload)


def timezone_at(lat: float, lon: float) -> dict[str, Any]:
    """Resolve the IANA timezone and current local time for a coordinate pair."""
    lat, lon = validate_coordinates(lat, lon)
    payload = get_json(
        OPEN_METEO_URL,
        {"latitude": lat, "longitude": lon, "timezone": "auto", "current": "temperature_2m"},
        # Local time moves, so a cached offset would go stale across a DST
        # boundary. The zone itself is stable but the wall clock is not.
        use_cache=False,
    )
    if not isinstance(payload, dict) or "timezone" not in payload:
        raise UpstreamError("Open-Meteo returned no timezone for this location")

    offset_seconds = int(payload.get("utc_offset_seconds") or 0)
    now_utc = datetime.now(dt_timezone.utc)
    local = now_utc.astimezone(dt_timezone(timedelta(seconds=offset_seconds)))
    hours, remainder = divmod(abs(offset_seconds), 3600)
    sign = "+" if offset_seconds >= 0 else "-"

    return {
        "timezone": _clean(payload.get("timezone")),
        "abbreviation": _clean(payload.get("timezone_abbreviation")),
        "utc_offset": f"{sign}{hours:02d}:{remainder // 60:02d}",
        "utc_offset_seconds": offset_seconds,
        "local_time": local.isoformat(timespec="seconds"),
        "utc_time": now_utc.isoformat(timespec="seconds"),
        "latitude": lat,
        "longitude": lon,
    }


def resolve_place(query: str) -> dict[str, Any]:
    """Geocode `query` and return the single best match.

    Raises UpstreamError when nothing matches, so callers that need one definite
    point (e.g. resolving `--home`) do not have to re-check for an empty list.
    """
    matches = geocode(query, limit=1)
    if not matches:
        raise UpstreamError(f"no location found for {query!r}")
    return matches[0]
