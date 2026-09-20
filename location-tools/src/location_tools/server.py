"""MCP server exposing location tools over stdio."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, config, places
from .geo import UNITS, CoordinateError, describe_distance, parse_coordinates, validate_coordinates
from .http_client import UpstreamError
from .ipgeo import locate_ip

server = MCPServer(name="location-tools", version=__version__)

# The model must not present an IP-derived position as the user's actual
# whereabouts, which is the default failure mode of a tool called
# "current_location". Stated on every tool that can return one.
_IP_CAVEAT = (
    "IMPORTANT: this server has no GPS. Unless an operator configured a fixed home "
    "position, the result is inferred from the public IP of the machine running this "
    "server, which is city-level at best and can be wrong by thousands of km behind a "
    "VPN or carrier NAT. Report the returned 'source' and 'accuracy_note' rather than "
    "stating the position as fact, and ask the user if precision matters."
)


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, **extra}


def _resolve_point(location: str, lat: float | None, lon: float | None) -> tuple[float, float, str]:
    """Resolve one point from either a place name/coordinate string or a lat/lon pair.

    Returns (lat, lon, label). Accepting both forms in one argument pair keeps the
    distance tool usable without forcing the model to geocode first.
    """
    if lat is not None and lon is not None:
        vlat, vlon = validate_coordinates(lat, lon)
        return vlat, vlon, f"{vlat},{vlon}"

    text = (location or "").strip()
    if not text:
        raise CoordinateError("provide either a location name/coordinates or both lat and lon")

    try:
        vlat, vlon = parse_coordinates(text)
        return vlat, vlon, f"{vlat},{vlon}"
    except CoordinateError:
        # Not a coordinate pair, so treat it as a place name to geocode.
        match = places.resolve_place(text)
        return match["latitude"], match["longitude"], match.get("short_label") or text


@server.tool(
    name="current_location",
    title="Current location",
    description=(
        "Get the approximate current location of the machine running this server, with "
        "city, region, country, coordinates, and timezone. Use for 'where am I' and to "
        "supply a default location to other tools. " + _IP_CAVEAT
    ),
)
def current_location(include_address: bool = False) -> dict[str, Any]:
    """Get the approximate current location.

    Args:
        include_address: Also reverse geocode the position to a street address.
            Costs one extra upstream call; the coordinates are usually too coarse
            for the street to be meaningful.
    """
    configured = config.settings.home
    if configured is not None:
        lat, lon = configured
        payload: dict[str, Any] = {
            "source": "configured",
            "latitude": lat,
            "longitude": lon,
            "accuracy_note": (
                "operator-configured fixed position (LOCATION_TOOLS_HOME), not an IP estimate"
            ),
        }
        if config.settings.home_label:
            payload["label"] = config.settings.home_label
    else:
        try:
            summary, warnings = locate_ip()
        except UpstreamError as exc:
            return _error(
                f"{exc}. No fixed position is configured either; set LOCATION_TOOLS_HOME="
                "'lat,lon' (or --home) to make this deterministic."
            )
        payload = {"source": "ip_geolocation", **summary}
        if warnings:
            payload["warnings"] = warnings

    try:
        payload["timezone_info"] = places.timezone_at(payload["latitude"], payload["longitude"])
    except (UpstreamError, CoordinateError) as exc:
        payload.setdefault("warnings", []).append(f"timezone lookup failed: {exc}")

    if include_address:
        try:
            payload["address"] = places.reverse_geocode(payload["latitude"], payload["longitude"])
        except (UpstreamError, CoordinateError) as exc:
            payload.setdefault("warnings", []).append(f"reverse geocode failed: {exc}")

    return payload


@server.tool(
    name="location_details",
    title="Location details",
    description=(
        "Resolve a place name, address, or coordinate pair into full details: "
        "coordinates, address components, bounding box, and timezone. Accepts "
        "'Kyoto', '1600 Pennsylvania Ave', or '35.02,135.75'. Omit the argument to "
        "describe the current location. " + _IP_CAVEAT
    ),
)
def location_details(location: str = "", include_timezone: bool = True) -> dict[str, Any]:
    """Get full details for a location.

    Args:
        location: Place name, address, or 'lat,lon'. Empty uses the current
            location (see current_location for its accuracy caveats).
        include_timezone: Resolve the IANA timezone and local time (default true).
    """
    text = (location or "").strip()
    source = "query"

    try:
        if not text:
            current = current_location()
            if "error" in current:
                return current
            lat, lon = current["latitude"], current["longitude"]
            source = current["source"]
        else:
            try:
                lat, lon = parse_coordinates(text)
                source = "coordinates"
            except CoordinateError:
                match = places.resolve_place(text)
                lat, lon = match["latitude"], match["longitude"]
    except (UpstreamError, CoordinateError) as exc:
        return _error(str(exc), location=text)

    payload: dict[str, Any] = {"source": source, "latitude": lat, "longitude": lon}
    if text:
        payload["query"] = text

    try:
        payload["place"] = places.reverse_geocode(lat, lon)
    except (UpstreamError, CoordinateError) as exc:
        payload.setdefault("warnings", []).append(f"address lookup failed: {exc}")

    if include_timezone:
        try:
            payload["timezone_info"] = places.timezone_at(lat, lon)
        except (UpstreamError, CoordinateError) as exc:
            payload.setdefault("warnings", []).append(f"timezone lookup failed: {exc}")

    if source == "ip_geolocation":
        payload["accuracy_note"] = (
            "derived from IP geolocation of this server's host; the address is only as "
            "good as that estimate"
        )
    return payload


@server.tool(
    name="ip_location",
    title="Geolocate an IP address",
    description=(
        "Geolocate a specific IP address, querying several providers and reporting how well "
        "they agree. Use for an IP you were given; use current_location for this host. "
        "Accuracy is inherently limited - the result reflects network registration, not a "
        "device position, and a VPN or proxy IP resolves to the exit node."
    ),
)
def ip_location(ip: str) -> dict[str, Any]:
    """Geolocate an IP address.

    Args:
        ip: IPv4 or IPv6 address to look up.
    """
    target = (ip or "").strip()
    if not target:
        return _error("ip must not be empty; use current_location to locate this host")
    # Reject anything with URL or path syntax before it reaches a provider, so a
    # mistaken argument fails here with a clear message.
    if any(char in target for char in "/?#@ "):
        return _error(f"'{target}' is not a bare IP address")

    try:
        summary, warnings = locate_ip(target)
    except UpstreamError as exc:
        return _error(str(exc), ip=target)

    payload: dict[str, Any] = {"query_ip": target, **summary}
    if warnings:
        payload["warnings"] = warnings
    return payload


@server.tool(
    name="geocode",
    title="Geocode a place name",
    description=(
        "Convert a place name or address into coordinates, returning ranked candidates. "
        "Use when a query is ambiguous and you need to see the alternatives ('Springfield', "
        "'Cambridge') rather than one assumed answer."
    ),
)
def geocode_tool(query: str, limit: int = 5, country_codes: str = "") -> dict[str, Any]:
    """Convert a place name or address into coordinates.

    Args:
        query: Place name or address, e.g. 'Kyoto' or '1600 Pennsylvania Ave NW'.
        limit: Maximum candidates to return (1-10, default 5).
        country_codes: Comma-separated ISO 3166-1 alpha-2 codes to restrict the
            search, e.g. 'us' or 'gb,ie'. Empty searches worldwide.
    """
    try:
        matches = places.geocode(query, limit=limit, country_codes=country_codes)
    except (UpstreamError, ValueError) as exc:
        return _error(str(exc), query=query, results=[], count=0)

    payload: dict[str, Any] = {"query": query, "count": len(matches), "results": matches}
    if not matches:
        payload["note"] = (
            "no match; try a less specific query, correct the spelling, or drop "
            "country_codes if it was set"
        )
    return payload


@server.tool(
    name="reverse_geocode",
    title="Reverse geocode coordinates",
    description=(
        "Convert coordinates into the nearest address, with full components (road, city, "
        "state, postcode, country). Use to turn a lat/lon back into a human-readable place."
    ),
)
def reverse_geocode_tool(
    latitude: float, longitude: float, zoom: int = places.DEFAULT_REVERSE_ZOOM
) -> dict[str, Any]:
    """Convert coordinates into an address.

    Args:
        latitude: Latitude in decimal degrees (-90 to 90).
        longitude: Longitude in decimal degrees (-180 to 180).
        zoom: Detail level 0-18; 18 is building level, 10 city, 5 state (default 18).
    """
    try:
        return {"place": places.reverse_geocode(latitude, longitude, zoom=zoom)}
    except (UpstreamError, CoordinateError) as exc:
        return _error(str(exc), latitude=latitude, longitude=longitude)


@server.tool(
    name="location_timezone",
    title="Timezone at a location",
    description=(
        "Get the IANA timezone, UTC offset, and current local time at a place or coordinate "
        "pair. Use for scheduling across timezones or to answer what time it is somewhere."
    ),
)
def location_timezone(
    location: str = "", latitude: float | None = None, longitude: float | None = None
) -> dict[str, Any]:
    """Get the timezone and current local time at a location.

    Args:
        location: Place name or 'lat,lon'. Empty uses the current location.
        latitude: Latitude in decimal degrees, if giving coordinates directly.
        longitude: Longitude in decimal degrees, if giving coordinates directly.
    """
    try:
        if not (location or "").strip() and latitude is None and longitude is None:
            current = current_location()
            if "error" in current:
                return current
            # current_location already resolved the timezone; reuse it.
            if "timezone_info" in current:
                return {"source": current["source"], **current["timezone_info"]}
            lat, lon, label = current["latitude"], current["longitude"], "current location"
        else:
            lat, lon, label = _resolve_point(location, latitude, longitude)
        return {"location": label, **places.timezone_at(lat, lon)}
    except (UpstreamError, CoordinateError, ValueError) as exc:
        return _error(str(exc), location=location)


@server.tool(
    name="distance",
    title="Distance between two locations",
    description=(
        "Great-circle (as-the-crow-flies) distance and initial compass bearing between two "
        "points. Each point is a place name or coordinates. Omit the origin to measure from "
        "the current location. NOT driving distance - this ignores roads entirely."
    ),
)
def distance_tool(
    to: str = "",
    origin: str = "",
    to_latitude: float | None = None,
    to_longitude: float | None = None,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    unit: str = "km",
) -> dict[str, Any]:
    """Distance and bearing between two points.

    Args:
        to: Destination as a place name or 'lat,lon'.
        origin: Origin as a place name or 'lat,lon'. Empty uses the current location.
        to_latitude: Destination latitude, if giving coordinates directly.
        to_longitude: Destination longitude, if giving coordinates directly.
        origin_latitude: Origin latitude, if giving coordinates directly.
        origin_longitude: Origin longitude, if giving coordinates directly.
        unit: Output unit: 'km', 'mi', or 'nmi' (default 'km').
    """
    unit = (unit or "km").strip().lower()
    if unit not in UNITS:
        return _error(f"invalid unit '{unit}'; use one of {UNITS}")

    notes: list[str] = []
    try:
        dest_lat, dest_lon, dest_label = _resolve_point(to, to_latitude, to_longitude)

        if not (origin or "").strip() and origin_latitude is None and origin_longitude is None:
            current = current_location()
            if "error" in current:
                return current
            origin_lat, origin_lon = current["latitude"], current["longitude"]
            origin_label = current.get("label") or current.get("city") or "current location"
            if current["source"] == "ip_geolocation":
                notes.append(
                    "origin is this server's IP-estimated location, so the distance inherits "
                    f"that error ({current.get('accuracy_note', 'accuracy unknown')})"
                )
        else:
            origin_lat, origin_lon, origin_label = _resolve_point(
                origin, origin_latitude, origin_longitude
            )
    except (UpstreamError, CoordinateError, ValueError) as exc:
        return _error(str(exc))

    payload = describe_distance(origin_lat, origin_lon, dest_lat, dest_lon, unit)
    payload["from"]["label"] = origin_label
    payload["to"]["label"] = dest_label
    if notes:
        payload["notes"] = notes
    return payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="location-tools",
        description="MCP server providing keyless location and geocoding tools.",
    )
    parser.add_argument(
        "--home",
        help=(
            "Fixed position for current_location, as 'lat,lon' or a place name to geocode "
            "at startup. Strongly recommended: without it the server falls back to IP "
            "geolocation, which is city-level at best. Also settable via LOCATION_TOOLS_HOME "
            "(coordinates only)."
        ),
    )
    parser.add_argument(
        "--home-label",
        help=(
            "Human-readable name for --home, e.g. 'Home, Berlin'. Also settable via "
            "LOCATION_TOOLS_HOME_LABEL."
        ),
    )
    parser.add_argument(
        "--user-agent",
        help=(
            "User-Agent sent upstream. Nominatim's policy requires one identifying the "
            "application. Also settable via LOCATION_TOOLS_USER_AGENT."
        ),
    )
    parser.add_argument(
        "--nominatim-url",
        help=(
            "Base URL for Nominatim, to point at a self-hosted instance. Also settable via "
            "LOCATION_TOOLS_NOMINATIM_URL."
        ),
    )
    parser.add_argument(
        "--language",
        help=(
            "Language for returned place names, as an Accept-Language value, default 'en'. "
            "Without it Nominatim answers in the local language of whatever was queried. "
            "Also settable via LOCATION_TOOLS_LANGUAGE."
        ),
    )
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        help=(
            "Self-imposed rate limit, default 60. Nominatim's public instance allows at most "
            "1 request/second. Also settable via LOCATION_TOOLS_REQUESTS_PER_MINUTE."
        ),
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        help=(
            "Seconds to cache geocoding responses, default 900. 0 disables caching. Also "
            "settable via LOCATION_TOOLS_CACHE_TTL."
        ),
    )
    parser.add_argument(
        "--ca-certs",
        metavar="PATH",
        help=(
            "PEM CA bundle used to verify outbound TLS. Needed behind a TLS-intercepting "
            "proxy with a private CA, since httpx does not read SSL_CERT_FILE. Also settable "
            "via LOCATION_TOOLS_CA_CERTS."
        ),
    )
    parser.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help=(
            "Disable outbound TLS certificate verification. Insecure; prefer --ca-certs. "
            "Also settable via LOCATION_TOOLS_SSL_VERIFY=0."
        ),
    )
    return parser.parse_args(argv)


def _resolve_home_arg(raw: str) -> tuple[tuple[float, float] | None, str]:
    """Resolve --home, which may be coordinates or a place name.

    Returns (coordinates, label). A geocode failure is reported and downgraded to
    IP fallback rather than aborting startup: a transient Nominatim outage should
    not stop the server from coming up.
    """
    try:
        return parse_coordinates(raw), ""
    except CoordinateError:
        pass
    try:
        match = places.resolve_place(raw)
    except (UpstreamError, ValueError) as exc:
        print(
            f"Warning: could not geocode --home {raw!r} ({exc}); "
            "falling back to IP geolocation",
            file=sys.stderr,
        )
        return None, ""
    return (match["latitude"], match["longitude"]), match.get("short_label", "")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    ssl_verify: bool | str | None = None
    if args.no_ssl_verify:
        ssl_verify = False
    elif args.ca_certs:
        ssl_verify = args.ca_certs

    home: tuple[float, float] | None = None
    home_label: str | None = args.home_label
    if args.home:
        home, derived_label = _resolve_home_arg(args.home.strip())
        if home is not None and not home_label:
            home_label = derived_label or args.home.strip()

    current = config.override(
        home=home,
        home_label=home_label,
        user_agent=args.user_agent,
        nominatim_url=args.nominatim_url.rstrip("/") if args.nominatim_url else None,
        accept_language=args.language,
        requests_per_minute=args.requests_per_minute,
        cache_ttl=args.cache_ttl,
        ssl_verify=ssl_verify,
    )

    print(f"location-tools {__version__} starting:", file=sys.stderr)
    if current.home is not None:
        label = f" ({current.home_label})" if current.home_label else ""
        print(f"  home: {current.home[0]},{current.home[1]}{label}", file=sys.stderr)
    else:
        print(
            "  home: unset - current_location will fall back to IP geolocation "
            "(city-level at best; set --home for accuracy)",
            file=sys.stderr,
        )
    print(
        f"  rate_limit={current.requests_per_minute}/min cache_ttl={current.cache_ttl:g}s",
        file=sys.stderr,
    )
    print(f"  nominatim={current.nominatim_url}", file=sys.stderr)
    if current.ssl_verify is not True:
        print(f"  ssl_verify={current.ssl_verify}", file=sys.stderr)

    server.run(transport="stdio")
