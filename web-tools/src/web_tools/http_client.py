"""HTTP transport: client construction, rate limiting, and the curl fallback.

Two backends live here. `httpx` is the plain client and handles most traffic.
`curl` routes through curl_cffi with a Chrome TLS fingerprint, which is the only
thing that gets past fingerprint-based bot filters - DuckDuckGo now answers
"wrong" TLS handshakes with an empty HTTP 202, and Cloudflare serves an
interstitial at HTTP 200. Neither trips `raise_for_status`, so both are detected
by inspecting the response rather than the status alone.

curl_cffi is an optional dependency (`web-tools[browser]`); every path that
needs it degrades to a readable message when it is absent.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# A bare User-Agent is itself a signal. These are the headers a real Chrome
# navigation sends; sending the UA without them is a mismatch bot filters notice.
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}

# The browser build curl_cffi impersonates. Kept as one constant so the search
# and fetch paths never drift onto different fingerprints.
IMPERSONATE = "chrome131"

CURL_MISSING_HINT = (
    "the 'curl' backend requires curl_cffi, which is not installed. Add the browser "
    "extra to the launch command: uvx --with 'curl_cffi>=0.7' --from <source> "
    "web-tools (or pip install 'web-tools[browser]' from a checkout)"
)


class CurlUnavailable(RuntimeError):
    """Raised when the curl backend is needed but curl_cffi is not installed."""


def curl_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


class RateLimiter:
    """Sliding-window limiter shared across tool calls.

    Search endpoints start challenging a single IP well before they start
    returning errors, so throttling ourselves keeps the server usable for longer
    than retrying into a block would.
    """

    def __init__(self, requests_per_minute: int | Callable[[], int] = 30) -> None:
        # Accepts a callable so the limit can track settings that CLI flags
        # rebind after this object is constructed at import time.
        self._limit = requests_per_minute
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def requests_per_minute(self) -> int:
        return self._limit() if callable(self._limit) else self._limit

    def acquire(self) -> None:
        limit = self.requests_per_minute
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60:
                    self._times.popleft()
                if len(self._times) < limit:
                    self._times.append(now)
                    return
                wait = 60 - (now - self._times[0])
            time.sleep(max(wait, 0.01))


def build_client(timeout: float = 20.0, verify: bool | str = True) -> httpx.Client:
    """Construct the httpx client.

    A single seam so tests can inject a mock transport without monkeypatching the
    httpx module globally.
    """
    return httpx.Client(
        timeout=timeout, follow_redirects=True, headers=BASE_HEADERS, verify=verify
    )


def curl_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 20.0,
    verify: bool | str = True,
    allow_redirects: bool = True,
) -> tuple[int, str, dict[str, str]]:
    """Issue one request through curl_cffi. Returns (status, body, headers).

    curl_cffi supplies its own impersonated browser headers; adding ours on top
    would produce a header order and casing that no real Chrome sends, defeating
    the point of impersonating one.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise CurlUnavailable(CURL_MISSING_HINT) from exc

    with curl_requests.Session(impersonate=IMPERSONATE, verify=verify) as session:
        response = session.request(
            method,
            url,
            params=params,
            data=data,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
    return response.status_code, response.text, dict(response.headers)
