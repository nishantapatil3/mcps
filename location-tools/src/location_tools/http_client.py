"""HTTP transport: client construction, rate limiting, and response caching.

All upstreams here are free, keyless, community-run services. Nominatim's usage
policy caps callers at 1 request/second and the IP providers throttle
aggressively on a shared IP, so being a well-behaved client is a functional
requirement rather than a courtesy: exceeding it gets the host blocked, not
queued. The rate limiter and the TTL cache both exist for that reason.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

import httpx

# Cache entries are small dicts; a few hundred is ample for a session and bounds
# memory without needing eviction tuning.
MAX_CACHE_ENTRIES = 256


class UpstreamError(RuntimeError):
    """Raised when an upstream service cannot be reached or returns junk."""


class RateLimiter:
    """Sliding-window limiter shared across tool calls.

    Blocks rather than rejecting: a geocode that arrives a second late is useful,
    whereas one refused locally is not, and the alternative (hammering upstream)
    ends in a block that outlasts any single call.
    """

    def __init__(self, requests_per_minute: int | Callable[[], int] = 60) -> None:
        # Accepts a callable so the limit can track settings that CLI flags
        # rebind after this object is constructed at import time.
        self._limit = requests_per_minute
        self._times: list[float] = []
        self._lock = threading.Lock()

    @property
    def requests_per_minute(self) -> int:
        return self._limit() if callable(self._limit) else self._limit

    def acquire(self) -> None:
        while True:
            limit = self.requests_per_minute
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60]
                if len(self._times) < limit:
                    self._times.append(now)
                    return
                wait = 60 - (now - self._times[0])
            time.sleep(max(wait, 0.01))


class TTLCache:
    """Small LRU+TTL cache for idempotent GET responses.

    Geocoding answers are stable over a session, and repeated identical lookups
    are the normal shape of agent traffic (the model re-resolves the same place
    across turns). Serving those from memory is what keeps us inside the upstream
    policy.
    """

    def __init__(self, ttl: float | Callable[[], float] = 900.0) -> None:
        self._ttl = ttl
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl() if callable(self._ttl) else self._ttl

    def get(self, key: str) -> Any | None:
        ttl = self.ttl
        if ttl <= 0:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at >= ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > MAX_CACHE_ENTRIES:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def build_client(
    timeout: float = 10.0, verify: bool | str = True, user_agent: str = ""
) -> httpx.Client:
    """Construct the httpx client.

    A single seam so tests can inject a mock transport without monkeypatching the
    httpx module globally.
    """
    headers = {"Accept": "application/json"}
    if user_agent:
        headers["User-Agent"] = user_agent
    return httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, verify=verify)
