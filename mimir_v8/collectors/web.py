"""Web page collector for Mímir v8.2.

Fetches a URL, extracts readable content, and returns a CollectResult
ready for ingestion into the learning pipeline.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError


TZ = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (compatible; MimirCollector/2.0; +https://github.com/mimir-memory/mimir)"


def _validate_url_safety(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL hostname is missing")
    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError("access to local addresses is restricted")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"access to private/reserved IP {hostname} is restricted")
    except ValueError as e:
        if "is restricted" in str(e):
            raise
        # Hostname is a domain name, resolve to check target IP
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for item in addr_info:
                sockaddr = item[4]
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError(f"resolved address {ip_str} is private/restricted")
        except socket.gaierror as gai_err:
            raise ValueError(f"cannot resolve hostname {hostname}: {gai_err}")


class WebCollector(BaseCollector):
    """Fetch a single URL and extract readable content."""

    def __init__(
        self,
        name: str = "web",
        enabled: bool = True,
        max_length: int = 10000,
    ):
        super().__init__(name, enabled)
        self.max_length = max_length

    def collect(self) -> list[CollectResult]:
        raise NotImplementedError("WebCollector collects one URL at a time via collect_url()")

    def collect_url(self, url: str, category: str = "knowledge") -> CollectResult:
        if not self.enabled:
            return CollectResult(errors=["collector disabled"])
        try:
            _validate_url_safety(url)
            content, title = self._fetch(url)
            result = CollectResult(
                title=title,
                url=url,
                content=content,
                items_collected=1,
            )
            return result
        except Exception as e:
            return CollectResult(url=url, errors=[str(e)])

    def describe(self) -> dict[str, Any]:
        return {
            "type": "web",
            "name": self.name,
            "enabled": self.enabled,
            "max_length": self.max_length,
        }

    def _fetch(self, url: str) -> tuple[str, str]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

        body = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()

        return body[:self.max_length], title[:200]