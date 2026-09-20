"""Canned upstream payloads, trimmed to the fields the parsers read."""

from __future__ import annotations

IPWHO_OK = {
    "ip": "203.0.113.7",
    "success": True,
    "city": "Kyoto",
    "region": "Kyoto",
    "country": "Japan",
    "country_code": "JP",
    "latitude": 35.0211,
    "longitude": 135.7538,
    "postal": "600-8216",
    "timezone": {"id": "Asia/Tokyo"},
    "connection": {"org": "Example ISP"},
}

IPINFO_OK = {
    "ip": "203.0.113.7",
    "city": "Kyoto",
    "region": "Kyoto",
    "country": "JP",
    "loc": "35.0250,135.7600",
    "postal": "600-8216",
    "timezone": "Asia/Tokyo",
    "org": "AS64496 Example ISP",
}

GEOJS_OK = {
    "ip": "203.0.113.7",
    "city": "Kyoto",
    "region": "Kyoto",
    "country": "Japan",
    "country_code": "JP",
    # Strings on purpose: geojs returns coordinates as strings.
    "latitude": "35.0190",
    "longitude": "135.7500",
    "timezone": "Asia/Tokyo",
    "organization_name": "Example ISP",
    "accuracy": 5,
}

NOMINATIM_PLACE = {
    "place_id": 1,
    "osm_type": "way",
    "osm_id": 42,
    "lat": "35.0210",
    "lon": "135.7538",
    "category": "place",
    "type": "city",
    "place_rank": 16,
    "name": "Kyoto",
    "display_name": "Kyoto, Kyoto Prefecture, Japan",
    "address": {
        "city": "Kyoto",
        "state": "Kyoto Prefecture",
        "country": "Japan",
        "country_code": "jp",
        "postcode": "600-8216",
    },
    "boundingbox": ["34.8747", "35.3175", "135.5647", "135.8679"],
}

OPEN_METEO_TZ = {
    "latitude": 35.0,
    "longitude": 135.75,
    "timezone": "Asia/Tokyo",
    "timezone_abbreviation": "GMT+9",
    "utc_offset_seconds": 32400,
}
