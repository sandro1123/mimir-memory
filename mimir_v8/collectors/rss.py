"""RSS feed collector for Mímir v8.2.

Fetches RSS/Atom feeds, deduplicates by URL, and returns CollectResults
ready for ingestion into the learning pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError


CACHE_DIR = Path.home() / ".hermes" / "mimir" / "collect"
CACHE_FILE = CACHE_DIR / "rss_seen_urls.json"
TZ = timezone(timedelta(hours=8))


class RSSCollector(BaseCollector):
    """Poll RSS/Atom feeds and collect new articles."""

    def __init__(
        self,
        feeds: list[dict] | None = None,
        name: str = "rss",
        enabled: bool = True,
        max_items_per_feed: int = 20,
        cache_size: int = 10000,
    ):
        super().__init__(name, enabled)
        self.feeds = feeds or []
        self.max_items_per_feed = max_items_per_feed
        self.cache_size = cache_size
        self._seen = self._load_seen()

    def configure(self, feeds: list[dict]) -> None:
        self.feeds = feeds

    def collect(self) -> list[CollectResult]:
        if not self.enabled:
            return []
        if not self.feeds:
            return []

        results = []
        for feed in self.feeds:
            name = feed.get("name", "?")
            url = feed.get("url", "")
            category = feed.get("category", "knowledge")
            if not url:
                continue
            try:
                items = self._fetch_feed(url, category)
                result = CollectResult(
                    title=name,
                    url=url,
                    content="",
                    items_collected=len(items),
                    items_skipped=0,
                )
                result._items = items
                results.append(result)
            except Exception as e:
                results.append(CollectResult(
                    title=name,
                    url=url,
                    errors=[str(e)],
                ))
        self._save_seen()
        return results

    def get_items(self, result: CollectResult) -> list[dict]:
        """Get the actual collected items from a CollectResult."""
        return getattr(result, "_items", [])

    def describe(self) -> dict[str, Any]:
        return {
            "type": "rss",
            "name": self.name,
            "enabled": self.enabled,
            "feed_count": len(self.feeds),
            "feeds": [{"name": f.get("name"), "url": f.get("url")} for f in self.feeds],
            "cached_urls": len(self._seen),
        }

    def _fetch_feed(self, feed_url: str, category: str) -> list[dict]:
        resp = urllib.request.urlopen(feed_url, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(raw)

        items = []
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for entry in entries[:self.max_items_per_feed]:
            title = self._get_text(entry, "title", ns)
            link = self._get_link(entry, ns)
            description = self._get_text(entry, "description", ns) or self._get_text(entry, "atom:content", ns)

            if not link or link in self._seen:
                continue

            content = re.sub(r"<[^>]+>", " ", description)
            content = re.sub(r"\s+", " ", content).strip()
            content = self.truncate(content)

            item = {
                "title": title.strip()[:200],
                "url": link,
                "content": content,
                "source": "rss",
                "category": category,
                "feed_name": feed_url,
                "collected_at": datetime.now(TZ).isoformat(),
            }
            self._seen.add(link)
            items.append(item)

        return items

    @staticmethod
    def _get_text(parent, tag: str, ns: dict) -> str:
        elem = parent.find(tag)
        if elem is not None and elem.text:
            return elem.text
        elem = parent.find(f"atom:{tag}", ns)
        if elem is not None and elem.text:
            return elem.text
        return ""

    @staticmethod
    def _get_link(parent, ns: dict) -> str:
        link_elem = parent.find("link")
        if link_elem is not None:
            href = link_elem.text or link_elem.get("href", "")
            if href:
                return href
        link_elem = parent.find("atom:link", ns)
        if link_elem is not None:
            return link_elem.get("href", "")
        return ""

    def _load_seen(self) -> set[str]:
        if CACHE_FILE.exists():
            try:
                return set(json.loads(CACHE_FILE.read_text()))
            except Exception:
                return set()
        return set()

    def _save_seen(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(list(self._seen)[-self.cache_size:], ensure_ascii=False))