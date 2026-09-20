"""Server-wide settings, resolved once from the environment at import time.

Everything here is startup configuration rather than per-call input: an operator
running the server behind a TLS-intercepting proxy or on a trusted internal host
needs these knobs, but the model calling the tools must not be able to reach
them. Each has a `WEB_TOOLS_*` env var and a matching CLI flag in `server.main`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from enum import Enum

# HTTP client backends. "httpx" is the plain, always-available client. "curl"
# routes through curl_cffi's Chrome TLS impersonation, which gets past
# fingerprint-based bot filters. "auto" tries httpx and falls back to curl only
# when the response looks like a block.
BACKENDS = ("httpx", "curl", "auto")


class SafeSearch(Enum):
    """DuckDuckGo SafeSearch levels, as the values its `kp` form field expects."""

    STRICT = "1"
    MODERATE = "-1"
    OFF = "-2"


@dataclass(frozen=True)
class Settings:
    search_backend: str = "auto"
    fetch_backend: str = "auto"
    safe_search: SafeSearch = SafeSearch.MODERATE
    region: str = ""
    allow_private_urls: bool = False
    # Passed straight to the HTTP clients as `verify=`: True (default trust
    # store), a CA bundle path, or False to disable verification entirely.
    ssl_verify: bool | str = True
    requests_per_minute: int = 30


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_backend(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower() or default
    if value not in BACKENDS:
        print(f"Warning: invalid {name}='{value}', using '{default}'", file=sys.stderr)
        return default
    return value


def _resolve_ssl_verify() -> bool | str:
    """Resolve `verify=` from WEB_TOOLS_CA_CERTS / WEB_TOOLS_SSL_VERIFY.

    A CA bundle path is needed behind TLS-intercepting proxies with a private CA:
    httpx does not read SSL_CERT_FILE, so the bundle has to be handed to it.
    """
    if os.getenv("WEB_TOOLS_SSL_VERIFY", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    ca_certs = os.getenv("WEB_TOOLS_CA_CERTS", "").strip()
    if ca_certs:
        if not os.path.isfile(ca_certs):
            print(
                f"Warning: WEB_TOOLS_CA_CERTS path '{ca_certs}' does not exist; "
                "TLS requests will fail",
                file=sys.stderr,
            )
        return ca_certs
    return True


def _resolve_safe_search() -> SafeSearch:
    raw = os.getenv("WEB_TOOLS_SAFE_SEARCH", "MODERATE").strip().upper()
    try:
        return SafeSearch[raw]
    except KeyError:
        print(f"Warning: invalid WEB_TOOLS_SAFE_SEARCH='{raw}', using MODERATE", file=sys.stderr)
        return SafeSearch.MODERATE


def _resolve_rpm() -> int:
    raw = os.getenv("WEB_TOOLS_REQUESTS_PER_MINUTE", "30").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        print(
            f"Warning: invalid WEB_TOOLS_REQUESTS_PER_MINUTE='{raw}', using 30", file=sys.stderr
        )
        return 30


def from_env() -> Settings:
    return Settings(
        search_backend=_env_backend("WEB_TOOLS_SEARCH_BACKEND", "auto"),
        fetch_backend=_env_backend("WEB_TOOLS_FETCH_BACKEND", "auto"),
        safe_search=_resolve_safe_search(),
        region=os.getenv("WEB_TOOLS_REGION", "").strip(),
        allow_private_urls=_env_flag("WEB_TOOLS_ALLOW_PRIVATE_URLS"),
        ssl_verify=_resolve_ssl_verify(),
        requests_per_minute=_resolve_rpm(),
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
