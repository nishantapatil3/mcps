"""Shared test fixtures."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

import location_tools.fetch as fetch_mod
from location_tools import config
from location_tools.http_client import RateLimiter


@pytest.fixture(autouse=True)
def hermetic(request, monkeypatch):
    """Keep offline tests off the real network and out of shared module state.

    Both the response cache and the rate limiter are process-global. The cache
    would make tests order-dependent, and the limiter is worse: mocked requests
    cost no time, so a full suite burns through the per-minute allowance and some
    unlucky test then blocks on a real 60-second sleep. Reset both per test.

    `network` tests are exempt: they exercise the real stack, and the throttle is
    there to keep those calls inside the upstream usage policy.
    """
    if request.node.get_closest_marker("network"):
        # This fixture is a generator, so the exempt path must still yield.
        yield
        return
    fetch_mod.clear_cache()
    monkeypatch.setattr(fetch_mod, "_limiter", RateLimiter(lambda: 10_000))
    # A default-constructed Settings keeps each test independent of whatever
    # LOCATION_TOOLS_* vars happen to be set in the developer's shell.
    monkeypatch.setattr(config, "settings", config.Settings(user_agent="test-agent/1.0"))

    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted a real HTTP request")

    monkeypatch.setattr(fetch_mod, "_build_client", forbidden)
    yield
    fetch_mod.clear_cache()


@pytest.fixture
def mock_http(monkeypatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Route outbound HTTP through a mock transport.

    Patches the module's own `_build_client` seam rather than `httpx.Client`,
    which would recurse because the replacement itself constructs a client.
    """

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def build_client(timeout: float = 10.0) -> httpx.Client:
            return httpx.Client(
                transport=httpx.MockTransport(handler),
                timeout=timeout,
                follow_redirects=True,
                headers={"Accept": "application/json"},
            )

        monkeypatch.setattr(fetch_mod, "_build_client", build_client)

    return install


@pytest.fixture
def route(mock_http) -> Callable[[dict[str, Any]], None]:
    """Serve canned JSON per URL substring.

    Keyed on a substring so tests do not have to reproduce exact query strings.
    A value may be a dict/list (serialized as JSON 200) or an httpx.Response.
    """

    def install(routes: dict[str, Any], default_status: int = 404) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for fragment, payload in routes.items():
                if fragment in url:
                    if isinstance(payload, httpx.Response):
                        return payload
                    return httpx.Response(200, text=json.dumps(payload))
            return httpx.Response(default_status, text=json.dumps({"error": "unrouted"}))

        mock_http(handler)

    return install


@pytest.fixture
def capture(mock_http) -> Callable[[Any], dict[str, Any]]:
    """Record the query params of the last outbound request.

    For asserting on what this server *sent* upstream (clamped limits, forwarded
    filters) rather than what it did with the reply. Goes through monkeypatch so
    the seam is restored after the test.
    """

    def install(response_payload: Any = None) -> dict[str, Any]:
        recorded: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.clear()
            recorded.update(request.url.params)
            recorded["__url__"] = str(request.url)
            body = [] if response_payload is None else response_payload
            return httpx.Response(200, text=json.dumps(body))

        mock_http(handler)
        return recorded

    return install


@pytest.fixture
def route_fn(mock_http) -> Callable[[Callable[[httpx.Request], Any]], None]:
    """Serve JSON computed from the request, for query-dependent responses.

    `route` is keyed on the URL alone, which cannot vary the reply by query
    string - needed when one test resolves two different place names.
    """

    def install(responder: Callable[[httpx.Request], Any]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = responder(request)
            if isinstance(payload, httpx.Response):
                return payload
            return httpx.Response(200, text=json.dumps(payload))

        mock_http(handler)

    return install
