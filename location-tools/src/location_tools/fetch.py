"""Shared JSON fetch path: rate limiting, caching, and the httpx seam.

Kept separate from `ipgeo` and `places` so both go through one throttle and one
cache, and so tests have a single function to intercept.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import config
from .http_client import RateLimiter, TTLCache, UpstreamError, build_client

_limiter = RateLimiter(lambda: config.settings.requests_per_minute)
_cache = TTLCache(lambda: config.settings.cache_ttl)


def _build_client(timeout: float = 10.0) -> httpx.Client:
    """Seam for tests to inject a mock transport. See `http_client.build_client`."""
    return build_client(
        timeout, verify=config.settings.ssl_verify, user_agent=config.settings.user_agent
    )


def clear_cache() -> None:
    _cache.clear()


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
    use_cache: bool = True,
) -> Any:
    """GET `url` and parse JSON, with rate limiting and TTL caching.

    Raises UpstreamError for transport failures, error statuses, and bodies that
    are not JSON. Every caller treats an upstream as fallible, so failures are
    surfaced as one exception type rather than leaking httpx internals.
    """
    cache_key = f"{url}?{json.dumps(params or {}, sort_keys=True)}"
    if use_cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    _limiter.acquire()
    try:
        with _build_client(timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        # 429 is the shape of upstream throttling; name it so the caller can say
        # something more useful than "request failed".
        if status == 429:
            raise UpstreamError(
                f"{url} rate-limited the request (HTTP 429); retry shortly or lower "
                "--requests-per-minute"
            ) from exc
        raise UpstreamError(f"{url} returned HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"{url} returned a non-JSON response") from exc
    except ValueError as exc:  # httpx raises its own JSON errors on some versions
        raise UpstreamError(f"{url} returned a non-JSON response: {exc}") from exc

    if use_cache:
        _cache.put(cache_key, payload)
    return payload
