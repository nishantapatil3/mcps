"""SSRF guard for outbound page fetches.

`fetch_page` takes a URL chosen by the model, which may in turn have come from a
search result or a page it just read. Without a guard that is a confused-deputy
primitive: anything the MCP host can reach on its own network - a metadata
endpoint, an unauthenticated admin port - becomes readable through a tool call.
Default-deny anything that does not resolve to a public address.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedURLError(Exception):
    """Raised when a fetch target is not an allowed public http(s) destination."""


def validate_public_url(url: str) -> None:
    """Reject non-public fetch targets.

    Enforces http/https, then resolves the host and rejects any URL that maps to
    a loopback, private (RFC1918), CGNAT, link-local (including the
    169.254.169.254 cloud metadata endpoint), reserved, multicast, or
    unspecified address. Call this on the initial URL and on every redirect hop.

    Note: the host is resolved here and resolved again by the HTTP client when it
    connects, so an attacker controlling DNS could rebind between the two lookups
    (TOCTOU). Pinning the socket to the validated IP is out of scope; default-deny
    plus per-hop validation closes the practical vectors.
    """
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BlockedURLError(
            f"unsupported URL scheme '{parsed.scheme}://' (only http and https are allowed)"
        )

    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host")

    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise BlockedURLError(f"refusing to fetch loopback host '{host}'")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        # An out-of-range port raises only on attribute access; treat it as
        # blocked rather than letting it surface as an unexpected error.
        raise BlockedURLError(f"invalid port in URL '{url}': {exc}") from exc

    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve host '{host}': {exc}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before classifying.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        # `not is_global` is the catch-all - it also covers ranges the explicit
        # flags miss, e.g. RFC 6598 CGNAT 100.64.0.0/10 used by Tailscale and some
        # cluster fabrics. The explicit flags stay because a few ranges report
        # is_global=True yet are non-routable (NAT64 64:ff9b::/96, via is_reserved).
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedURLError(
                f"refusing to fetch '{host}' - it resolves to non-public address {ip}"
            )
