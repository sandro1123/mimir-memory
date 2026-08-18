"""Web page collector for Mímir v8.2.

Fetches a URL, extracts readable content, and returns a CollectResult
ready for ingestion into the learning pipeline.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError


TZ = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (compatible; MimirCollector/2.0; +https://github.com/mimir-memory/mimir)"


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