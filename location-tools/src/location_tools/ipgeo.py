"""IP-based geolocation across several keyless providers.

IP geolocation is an inference from routing and registry data, not a measurement.
It is commonly wrong by tens of kilometres and can be wrong by thousands: a VPN
reports the exit node, CGNAT reports the carrier's aggregation point, and a
provider with no data for a prefix often returns the country's geographic centre
rather than admitting ignorance.

Because of that, this module queries multiple providers and reports their
agreement instead of presenting one answer as fact. Consensus is genuine signal -
independent databases converging on a city is meaningful, while a 70km spread
tells the caller not to trust the number. A single provider could not distinguish
those two cases, and the caller would have no way to know which it got.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .fetch import get_json
from .geo import CoordinateError, haversine_km, validate_coordinates
from .http_client import UpstreamError

# Providers must be HTTPS and keyless. ip-api.com is deliberately excluded: its
# free tier is HTTP-only, which would leak the query and let any on-path observer
# rewrite the coordinates we then report as the host's location.
#
# Spread thresholds for the agreement summary, in kilometres. 25km is roughly
# "same metro area"; past 150km the providers are not describing the same place.
_TIGHT_SPREAD_KM = 25.0
_LOOSE_SPREAD_KM = 150.0

# Providers sometimes answer "somewhere in this country" with the country's
# centroid. geojs flags this with accuracy=1000; treat a radius this large as
# country-level rather than a real position.
_COUNTRY_LEVEL_ACCURACY_KM = 500


@dataclass(frozen=True)
class IPLocation:
    provider: str
    latitude: float
    longitude: float
    city: str = ""
    region: str = ""
    country: str = ""
    country_code: str = ""
    postal: str = ""
    timezone: str = ""
    organization: str = ""
    ip: str = ""
    # Provider-reported radius of uncertainty, when it gives one.
    accuracy_radius_km: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "provider": self.provider,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "city": self.city,
            "region": self.region,
            "country": self.country,
            "country_code": self.country_code,
            "postal": self.postal,
            "timezone": self.timezone,
            "organization": self.organization,
        }
        if self.ip:
            payload["ip"] = self.ip
        if self.accuracy_radius_km is not None:
            payload["accuracy_radius_km"] = self.accuracy_radius_km
        return {k: v for k, v in payload.items() if v != ""}


def _clean(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _coerce(lat: Any, lon: Any) -> tuple[float, float]:
    """Coerce provider lat/lon to validated floats.

    geojs returns them as strings while the others use numbers, so this cannot
    assume a type.
    """
    try:
        return validate_coordinates(float(lat), float(lon))
    except (TypeError, ValueError) as exc:
        raise UpstreamError(f"provider returned unusable coordinates ({lat!r}, {lon!r})") from exc


def _parse_ipwho(data: dict[str, Any]) -> IPLocation:
    # ipwho.is signals failure in the body at HTTP 200, so the status alone is
    # not enough to tell success from "Invalid IP address". The provider name is
    # added by the caller, so it is not repeated here.
    if data.get("success") is False:
        raise UpstreamError(_clean(data.get("message")) or "lookup failed")
    lat, lon = _coerce(data.get("latitude"), data.get("longitude"))
    connection = data.get("connection") or {}
    timezone = data.get("timezone") or {}
    return IPLocation(
        provider="ipwho.is",
        latitude=lat,
        longitude=lon,
        city=_clean(data.get("city")),
        region=_clean(data.get("region")),
        country=_clean(data.get("country")),
        country_code=_clean(data.get("country_code")),
        postal=_clean(data.get("postal")),
        timezone=_clean(timezone.get("id") if isinstance(timezone, dict) else timezone),
        organization=_clean(connection.get("org") or connection.get("isp")),
        ip=_clean(data.get("ip")),
    )


def _parse_ipinfo(data: dict[str, Any]) -> IPLocation:
    if data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise UpstreamError(_clean(message) or "lookup failed")
    loc = _clean(data.get("loc"))
    if "," not in loc:
        raise UpstreamError("response had no 'loc' coordinates")
    lat_text, lon_text = loc.split(",", 1)
    lat, lon = _coerce(lat_text, lon_text)
    return IPLocation(
        provider="ipinfo.io",
        latitude=lat,
        longitude=lon,
        city=_clean(data.get("city")),
        region=_clean(data.get("region")),
        country=_clean(data.get("country")),
        country_code=_clean(data.get("country")),  # ipinfo reports a 2-letter code here
        postal=_clean(data.get("postal")),
        timezone=_clean(data.get("timezone")),
        organization=_clean(data.get("org")),
        ip=_clean(data.get("ip")),
    )


def _parse_geojs(data: Any) -> IPLocation:
    # The explicit-IP endpoint returns a single-element list; the self endpoint
    # returns a bare object.
    if isinstance(data, list):
        if not data:
            raise UpstreamError("empty response")
        data = data[0]
    if not isinstance(data, dict):
        raise UpstreamError("unexpected response shape")
    lat, lon = _coerce(data.get("latitude"), data.get("longitude"))
    accuracy = data.get("accuracy")
    try:
        accuracy_km = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        accuracy_km = None
    return IPLocation(
        provider="geojs.io",
        latitude=lat,
        longitude=lon,
        city=_clean(data.get("city")),
        region=_clean(data.get("region")),
        country=_clean(data.get("country")),
        country_code=_clean(data.get("country_code")),
        timezone=_clean(data.get("timezone")),
        organization=_clean(data.get("organization_name") or data.get("organization")),
        ip=_clean(data.get("ip")),
        accuracy_radius_km=accuracy_km,
    )


@dataclass(frozen=True)
class _Provider:
    name: str
    self_url: str
    ip_url: str  # `{ip}` placeholder
    parser: Callable[[Any], IPLocation]

    def url_for(self, ip: str | None) -> str:
        return self.ip_url.format(ip=ip) if ip else self.self_url


_PROVIDERS = (
    _Provider("ipwho.is", "https://ipwho.is/", "https://ipwho.is/{ip}", _parse_ipwho),
    _Provider(
        "ipinfo.io", "https://ipinfo.io/json", "https://ipinfo.io/{ip}/json", _parse_ipinfo
    ),
    _Provider(
        "geojs.io",
        "https://get.geojs.io/v1/ip/geo.json",
        "https://get.geojs.io/v1/ip/geo/{ip}.json",
        _parse_geojs,
    ),
)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(locations: list[IPLocation]) -> dict[str, Any]:
    """Reduce provider answers to one position plus an honest agreement summary.

    The median is used rather than the mean because a single provider falling back
    to a country centroid is a large outlier, and a mean would drag the reported
    position toward it. With three providers the median simply discards it.
    """
    if not locations:
        raise UpstreamError("no IP geolocation provider returned a usable answer")

    lat = _median([loc.latitude for loc in locations])
    lon = _median([loc.longitude for loc in locations])

    # Spread is measured from the consensus point, so one outlier cannot hide
    # behind a pair that happens to agree with each other.
    spread = max((haversine_km(lat, lon, loc.latitude, loc.longitude) for loc in locations), default=0.0)

    if len(locations) == 1:
        agreement = "single-provider"
        note = (
            f"only {locations[0].provider} answered, so there is no cross-check on this "
            "position"
        )
    elif spread <= _TIGHT_SPREAD_KM:
        agreement = "close"
        note = f"{len(locations)} providers agree within {spread:.1f} km"
    elif spread <= _LOOSE_SPREAD_KM:
        agreement = "loose"
        note = (
            f"{len(locations)} providers disagree by up to {spread:.0f} km; the city-level "
            "answer is probably right but the coordinates are not"
        )
    else:
        agreement = "poor"
        note = (
            f"{len(locations)} providers disagree by up to {spread:.0f} km; treat this "
            "position as unreliable - a VPN or carrier-level NAT is the usual cause"
        )

    # A provider-declared radius this large means "somewhere in this country".
    country_level = [
        loc.provider
        for loc in locations
        if loc.accuracy_radius_km is not None
        and loc.accuracy_radius_km >= _COUNTRY_LEVEL_ACCURACY_KM
    ]

    # Prefer descriptive fields from whichever provider is nearest the consensus,
    # since that one is least likely to be the outlier.
    best = min(locations, key=lambda loc: haversine_km(lat, lon, loc.latitude, loc.longitude))

    def pick(attr: str) -> str:
        return _clean(getattr(best, attr)) or next(
            (_clean(getattr(loc, attr)) for loc in locations if _clean(getattr(loc, attr))), ""
        )

    summary: dict[str, Any] = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "city": pick("city"),
        "region": pick("region"),
        "country": pick("country"),
        "country_code": pick("country_code"),
        "postal": pick("postal"),
        "timezone": pick("timezone"),
        "organization": pick("organization"),
        "ip": pick("ip"),
        "agreement": agreement,
        "provider_spread_km": round(spread, 1),
        "providers": [loc.as_dict() for loc in locations],
        "accuracy_note": note,
    }
    if country_level:
        summary["accuracy_note"] += (
            f"; {', '.join(country_level)} reported only country-level accuracy"
        )
    return {k: v for k, v in summary.items() if v != ""}


def locate_ip(ip: str | None = None, *, timeout: float = 10.0) -> tuple[dict[str, Any], list[str]]:
    """Geolocate `ip`, or this host's public IP when None.

    Queries every provider and summarizes. Returns (summary, warnings); warnings
    name each provider that failed, so a degraded answer is visibly degraded
    rather than silently thinner.
    """
    locations: list[IPLocation] = []
    warnings: list[str] = []

    for provider in _PROVIDERS:
        try:
            locations.append(provider.parser(get_json(provider.url_for(ip), timeout=timeout)))
        except (UpstreamError, CoordinateError) as exc:
            warnings.append(f"{provider.name}: {exc}")
        except Exception as exc:  # a malformed payload must not sink the others
            warnings.append(f"{provider.name}: unexpected {type(exc).__name__}: {exc}")

    if not locations:
        detail = "; ".join(warnings) if warnings else "no provider was reachable"
        raise UpstreamError(f"every IP geolocation provider failed: {detail}")
    return summarize(locations), warnings
