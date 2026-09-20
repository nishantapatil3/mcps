"""Shared test fixtures."""

from __future__ import annotations

from typing import Callable

import httpx
import pytest

import web_tools.search as search_mod


@pytest.fixture(autouse=True)
def hermetic(request, monkeypatch):
    """Keep offline tests off the real network and off DNS.

    Two paths would otherwise escape the mock transport: the "auto" backend falls
    back to curl_cffi (a separate network stack the mock never sees), and the SSRF
    guard resolves hostnames for real. Both are exercised explicitly by the tests
    that care, via `allow_curl` and by patching the guard directly.

    Skipped for `network` tests, which are meant to exercise the real stack -
    including the real guard and the curl fallback if it is installed.
    """
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setattr(search_mod, "curl_available", lambda: False)
    monkeypatch.setattr(search_mod, "validate_public_url", lambda url: None)


@pytest.fixture
def allow_curl(monkeypatch) -> Callable[[Callable[..., tuple]], None]:
    """Enable the curl backend with a stub standing in for curl_cffi."""

    def install(handler: Callable[..., tuple]) -> None:
        monkeypatch.setattr(search_mod, "curl_available", lambda: True)
        monkeypatch.setattr(search_mod, "curl_request", handler)

    return install


@pytest.fixture
def mock_http(monkeypatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Route all outbound HTTP in the search module through a mock transport.

    Patches the module's own `_build_client` seam rather than `httpx.Client`,
    which would recurse because the replacement itself constructs a client.
    """

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def build_client(timeout: float = 20.0) -> httpx.Client:
            return httpx.Client(
                transport=httpx.MockTransport(handler),
                timeout=timeout,
                follow_redirects=True,
                headers=search_mod.BASE_HEADERS,
            )

        monkeypatch.setattr(search_mod, "_build_client", build_client)

    return install
