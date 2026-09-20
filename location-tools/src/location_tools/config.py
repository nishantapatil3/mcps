"""Server-wide settings, resolved once from the environment at import time.

Everything here is startup configuration rather than per-call input. The most
important knob is `home`: IP geolocation is the only automatic position source
available to a process with no GPS, and it is routinely wrong by tens of
kilometres (VPNs, CGNAT, carrier backhaul). An operator who knows where the host
actually is can state it once here, and `current_location` will prefer it over
guessing. Each setting has a `LOCATION_TOOLS_*` env var and a matching CLI flag
in `server.main`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace

from .geo import CoordinateError, parse_coordinates

# Nominatim's usage policy requires a User-Agent that identifies the
# application, and explicitly rejects stock library defaults. It also caps usage
# at 1 request/second, which `http_client.RateLimiter` enforces separately.
DEFAULT_USER_AGENT = "location-tools-mcp/{version} (+https://github.com/nishantapatil3/mcps)"


@dataclass(frozen=True)
class Settings:
    # Operator-declared position of this host, as (lat, lon). None means fall
    # back to IP geolocation and label the result as the estimate it is.
    home: tuple[float, float] | None = None
    # Free-text label for `home`, purely descriptive (e.g. "Office, Berlin").
    home_label: str = ""
    # Sent to Nominatim and the IP providers. Policy-mandated; see above.
    user_agent: str = ""
    # Passed straight to httpx as `verify=`: True (default trust store), a CA
    # bundle path, or False to disable verification entirely.
    ssl_verify: bool | str = True
    # Nominatim's documented ceiling is 1/sec. Kept configurable because a
    # self-hosted instance has no such limit.
    requests_per_minute: int = 60
    # Geocoding results are stable over a session; caching keeps us well inside
    # the upstream policy. Seconds.
    cache_ttl: float = 900.0
    # Base URL for Nominatim. Override to point at a self-hosted instance.
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    # Language for place names. Without this Nominatim returns names in the local
    # language of whatever was queried ("日本" for Japan, "Россия" for Russia),
    # so the output language would depend on the query rather than the caller.
    accept_language: str = "en"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_ssl_verify() -> bool | str:
    """Resolve `verify=` from LOCATION_TOOLS_CA_CERTS / LOCATION_TOOLS_SSL_VERIFY.

    A CA bundle path is needed behind TLS-intercepting proxies with a private CA:
    httpx does not read SSL_CERT_FILE, so the bundle has to be handed to it.
    """
    if os.getenv("LOCATION_TOOLS_SSL_VERIFY", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    ca_certs = os.getenv("LOCATION_TOOLS_CA_CERTS", "").strip()
    if ca_certs:
        if not os.path.isfile(ca_certs):
            print(
                f"Warning: LOCATION_TOOLS_CA_CERTS path '{ca_certs}' does not exist; "
                "TLS requests will fail",
                file=sys.stderr,
            )
        return ca_certs
    return True


def _resolve_home() -> tuple[float, float] | None:
    """Parse LOCATION_TOOLS_HOME as 'lat,lon'.

    Deliberately coordinates-only: accepting a place name here would mean a
    network geocode during import, which would make startup fail on a transient
    upstream outage. `server.main` resolves a name to coordinates instead, where
    a failure can be reported without killing the process.
    """
    raw = os.getenv("LOCATION_TOOLS_HOME", "").strip()
    if not raw:
        return None
    try:
        return parse_coordinates(raw)
    except CoordinateError as exc:
        print(
            f"Warning: invalid LOCATION_TOOLS_HOME='{raw}' ({exc}); "
            "falling back to IP geolocation",
            file=sys.stderr,
        )
        return None


def _resolve_positive(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        print(f"Warning: invalid {name}='{raw}', using {default}", file=sys.stderr)
        return default


def _resolve_rpm() -> int:
    raw = os.getenv("LOCATION_TOOLS_REQUESTS_PER_MINUTE", "60").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        print(
            f"Warning: invalid LOCATION_TOOLS_REQUESTS_PER_MINUTE='{raw}', using 60",
            file=sys.stderr,
        )
        return 60


def from_env() -> Settings:
    from . import __version__

    return Settings(
        home=_resolve_home(),
        home_label=os.getenv("LOCATION_TOOLS_HOME_LABEL", "").strip(),
        user_agent=os.getenv("LOCATION_TOOLS_USER_AGENT", "").strip()
        or DEFAULT_USER_AGENT.format(version=__version__),
        ssl_verify=_resolve_ssl_verify(),
        requests_per_minute=_resolve_rpm(),
        cache_ttl=_resolve_positive("LOCATION_TOOLS_CACHE_TTL", 900.0),
        nominatim_url=os.getenv("LOCATION_TOOLS_NOMINATIM_URL", "").strip().rstrip("/")
        or "https://nominatim.openstreetmap.org",
        accept_language=os.getenv("LOCATION_TOOLS_LANGUAGE", "").strip() or "en",
    )


# The live settings object. `server.main` rebinds this from CLI flags before the
# transport starts; everything downstream reads `config.settings` at call time so
# the override is picked up without threading a parameter through every function.
settings: Settings = from_env()


def override(**kwargs) -> Settings:
    """Replace the live settings with `kwargs` applied. Ignores None values."""
    global settings
    settings = replace(settings, **{k: v for k, v in kwargs.items() if v is not None})
    return settings
