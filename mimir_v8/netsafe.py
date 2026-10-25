"""Outbound HTTP safety for collectors and the dashboard (SSRF guard).

Every server-side fetch of a user- or feed-supplied URL goes through
:func:`safe_urlopen`. It:

* allows only ``http``/``https``;
* resolves the hostname and rejects loopback / private / link-local /
  reserved / multicast / unspecified addresses (IPv4 and IPv6, including
  IPv4-mapped IPv6);
* re-validates **every redirect hop** (``urllib`` follows 30x silently, so
  a public URL redirecting to ``http://169.254.169.254/`` used to pass);
* caps the number of bytes read (``max_bytes``) so a hostile endpoint
  cannot exhaust memory.

Residual risk: validation resolves DNS once and ``urllib`` resolves again
when connecting, so a DNS-rebinding attacker with a very short TTL still has
a narrow window. Deployments that fetch untrusted URLs should additionally
egress-filter at the network layer.

Intranet sources (an RSS feed on the NAS, a LAN wiki) are legitimate for a
self-hosted memory system, so private targets can be re-enabled explicitly
with ``MIMIR_ALLOW_PRIVATE_FETCH=1``.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
DEFAULT_TIMEOUT = 15
MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL (or one of its redirect hops) is not allowed."""


def _private_allowed() -> bool:
    return os.environ.get("MIMIR_ALLOW_PRIVATE_FETCH", "").strip().lower() in ("1", "true", "yes")


def _ip_is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # ``not is_global`` also covers 100.64.0.0/10 (CGNAT — Tailscale's
    # range), which ``is_private`` does not.
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` unless ``url`` is safe to fetch."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError(f"unsupported URL scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL hostname is missing")
    if _private_allowed():
        return
    if hostname.lower() in ("localhost", "localhost.localdomain") or hostname.lower().endswith(".localhost"):
        raise UnsafeURLError("access to local addresses is restricted")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_forbidden(literal):
            raise UnsafeURLError(f"access to private/reserved IP {hostname} is restricted")
        return
    try:
        infos = socket.getaddrinfo(hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve hostname {hostname}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if _ip_is_forbidden(ip):
            raise UnsafeURLError(f"resolved address {ip} is private/restricted")


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    # Built per call so proxy env vars (HTTP(S)_PROXY / NO_PROXY) are honoured
    # as they are at fetch time.
    return urllib.request.build_opener(_ValidatingRedirectHandler)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


def safe_urlopen(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch ``url`` with SSRF checks on every hop and a hard size cap."""
    validate_url(url)
    request = urllib.request.Request(url, headers=headers or {})
    with _opener().open(request, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
        truncated = len(body) > max_bytes
        return FetchResult(
            url=resp.geturl(),
            status=getattr(resp, "status", 200),
            content_type=resp.headers.get("Content-Type", ""),
            body=body[:max_bytes],
            truncated=truncated,
        )
