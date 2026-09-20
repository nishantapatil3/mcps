"""The SSRF guard on fetch_page.

These tests bypass the autouse `hermetic` fixture's no-op guard by calling
`validate_public_url` directly, and stub DNS so nothing resolves for real.
"""

from __future__ import annotations

import socket

import pytest

import web_tools.config as config
import web_tools.search as search_mod
import web_tools.ssrf as ssrf
from web_tools.server import fetch_page_tool
from web_tools.ssrf import BlockedURLError, validate_public_url


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin DNS resolution to a fixed address for every lookup."""

    def install(ip: str) -> None:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        monkeypatch.setattr(
            ssrf.socket,
            "getaddrinfo",
            lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, 443))],
        )

    return install


class TestSchemeAndHost:
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "gopher://x"])
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(BlockedURLError, match="scheme"):
            validate_public_url(url)

    def test_rejects_localhost_without_resolving(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not resolve a name it already knows is loopback")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", boom)
        with pytest.raises(BlockedURLError, match="loopback"):
            validate_public_url("http://localhost:8080/admin")

    def test_rejects_unresolvable_host(self, monkeypatch):
        monkeypatch.setattr(
            ssrf.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nx"))
        )
        with pytest.raises(BlockedURLError, match="could not resolve"):
            validate_public_url("https://nope.invalid")


class TestAddressClasses:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918
            "192.168.1.1",  # RFC1918
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # RFC6598 CGNAT (is_private is False here)
            "0.0.0.0",  # unspecified
            "224.0.0.1",  # multicast
            "::1",  # IPv6 loopback
            "fd00::1",  # IPv6 unique-local
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
        ],
    )
    def test_rejects_non_public(self, resolves_to, ip):
        resolves_to(ip)
        with pytest.raises(BlockedURLError, match="non-public"):
            validate_public_url("https://sneaky.example")

    def test_allows_public_address(self, resolves_to):
        resolves_to("93.184.216.34")
        validate_public_url("https://example.com")


class TestToolBehavior:
    def test_tool_reports_block_without_raising(self, monkeypatch):
        monkeypatch.setattr(
            search_mod,
            "validate_public_url",
            lambda url: (_ for _ in ()).throw(BlockedURLError("resolves to 127.0.0.1")),
        )
        out = fetch_page_tool("http://internal.example/admin")
        assert "SSRF" in out["error"]
        assert "allow-private-urls" in out["error"]

    def test_allow_private_urls_skips_the_guard(self, monkeypatch, mock_http):
        def boom(url):
            raise AssertionError("guard must not run when private URLs are allowed")

        monkeypatch.setattr(search_mod, "validate_public_url", boom)
        monkeypatch.setattr(config, "settings", config.settings.__class__(allow_private_urls=True))
        mock_http(lambda request: __import__("httpx").Response(200, text="<main>ok</main>"))
        assert search_mod.fetch_page("http://10.0.0.1/")["content"] == "ok"


class TestRedirectHops:
    def test_every_hop_is_validated(self, monkeypatch, mock_http):
        import httpx

        seen: list[str] = []
        monkeypatch.setattr(search_mod, "validate_public_url", lambda url: seen.append(url))

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "http://10.0.0.1/internal"})
            return httpx.Response(200, text="<main>secret</main>")

        mock_http(handler)
        search_mod.fetch_page("https://public.example/start")
        assert seen == ["https://public.example/start", "http://10.0.0.1/internal"]

    def test_redirect_loop_is_bounded(self, mock_http):
        import httpx

        mock_http(
            lambda request: httpx.Response(302, headers={"location": "https://a.example/loop"})
        )
        with pytest.raises(httpx.HTTPError, match="too many redirects"):
            search_mod.fetch_page("https://a.example/loop")
