"""Shared test fixtures."""

from __future__ import annotations

from typing import Callable

import httpx
import pytest

import web_search_mcp.search as search_mod


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
