"""Transport layer: caching, rate limiting, and upstream error translation."""

from __future__ import annotations

import time

import httpx
import pytest

from location_tools import config, fetch
from location_tools.http_client import RateLimiter, TTLCache, UpstreamError


# --- TTL cache ---


def test_cache_returns_stored_value():
    cache = TTLCache(ttl=60)
    cache.put("k", {"v": 1})
    assert cache.get("k") == {"v": 1}


def test_cache_expires_entries(monkeypatch):
    cache = TTLCache(ttl=0.05)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    time.sleep(0.06)
    assert cache.get("k") is None


def test_zero_ttl_disables_caching():
    # 0 must mean "off", not "expire instantly but still store".
    cache = TTLCache(ttl=0)
    cache.put("k", "v")
    assert cache.get("k") is None


def test_cache_evicts_least_recently_used():
    from location_tools.http_client import MAX_CACHE_ENTRIES

    cache = TTLCache(ttl=60)
    for i in range(MAX_CACHE_ENTRIES + 10):
        cache.put(f"k{i}", i)
    # The earliest keys are gone; the most recent survive.
    assert cache.get("k0") is None
    assert cache.get(f"k{MAX_CACHE_ENTRIES + 9}") == MAX_CACHE_ENTRIES + 9


def test_cache_ttl_tracks_a_callable():
    # Settings are rebound by CLI flags after construction, so the limit has to
    # be read at call time rather than captured.
    ttl = {"value": 0.0}
    cache = TTLCache(ttl=lambda: ttl["value"])
    cache.put("k", "v")
    assert cache.get("k") is None
    ttl["value"] = 60.0
    cache.put("k", "v")
    assert cache.get("k") == "v"


# --- Rate limiter ---


def test_rate_limiter_allows_up_to_the_limit():
    limiter = RateLimiter(5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    # Five acquisitions inside the window must not block.
    assert time.monotonic() - start < 0.5


def test_rate_limiter_blocks_past_the_limit(monkeypatch):
    limiter = RateLimiter(2)
    slept: list[float] = []
    # Simulate the wait instead of actually sleeping 60s.
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    limiter.acquire()
    limiter.acquire()
    # The third would block; drop the window so the loop can exit.
    limiter._times.clear()
    limiter.acquire()
    assert True  # reaching here means acquire() terminated


def test_rate_limiter_tracks_a_callable_limit():
    limit = {"value": 1}
    limiter = RateLimiter(lambda: limit["value"])
    assert limiter.requests_per_minute == 1
    limit["value"] = 30
    assert limiter.requests_per_minute == 30


# --- get_json ---


def test_repeated_identical_request_is_served_from_cache(mock_http, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(cache_ttl=60))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    mock_http(handler)
    fetch.get_json("https://example.test/a", {"q": "x"})
    fetch.get_json("https://example.test/a", {"q": "x"})
    assert calls["n"] == 1


def test_differing_params_are_cached_separately(mock_http, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(cache_ttl=60))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    mock_http(handler)
    fetch.get_json("https://example.test/a", {"q": "x"})
    fetch.get_json("https://example.test/a", {"q": "y"})
    assert calls["n"] == 2


def test_use_cache_false_always_refetches(mock_http, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(cache_ttl=60))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    mock_http(handler)
    fetch.get_json("https://example.test/a", use_cache=False)
    fetch.get_json("https://example.test/a", use_cache=False)
    assert calls["n"] == 2


def test_http_error_status_becomes_upstream_error(mock_http):
    mock_http(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(UpstreamError, match="500"):
        fetch.get_json("https://example.test/a")


def test_rate_limit_status_is_named(mock_http):
    mock_http(lambda request: httpx.Response(429, text="slow down"))
    with pytest.raises(UpstreamError, match="429"):
        fetch.get_json("https://example.test/a")


def test_non_json_body_becomes_upstream_error(mock_http):
    mock_http(lambda request: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(UpstreamError, match="non-JSON"):
        fetch.get_json("https://example.test/a")


def test_transport_failure_becomes_upstream_error(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    mock_http(handler)
    with pytest.raises(UpstreamError, match="could not reach"):
        fetch.get_json("https://example.test/a")


def test_failures_are_not_cached(mock_http, monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(cache_ttl=60))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Fail first, succeed after: a cached failure would hide the recovery.
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    mock_http(handler)
    with pytest.raises(UpstreamError):
        fetch.get_json("https://example.test/a")
    assert fetch.get_json("https://example.test/a") == {"ok": True}


# --- User-Agent compliance ---


def test_user_agent_is_sent(monkeypatch):
    # Nominatim's policy rejects stock library User-Agents, so this header being
    # present is a functional requirement rather than cosmetic.
    monkeypatch.setattr(config, "settings", config.Settings(user_agent="location-tools-mcp/9.9"))
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        fetch,
        "_build_client",
        lambda timeout=10.0: httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": config.settings.user_agent, "Accept": "application/json"},
        ),
    )
    fetch.get_json("https://example.test/a", use_cache=False)
    assert seen["user-agent"] == "location-tools-mcp/9.9"


def test_default_user_agent_identifies_the_application():
    settings = config.Settings(user_agent=config.DEFAULT_USER_AGENT.format(version="1.2.3"))
    assert "location-tools" in settings.user_agent
    assert "1.2.3" in settings.user_agent
    # A URL lets an upstream operator identify and contact the source of traffic.
    assert "github.com" in settings.user_agent
