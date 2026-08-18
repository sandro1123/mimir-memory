"""Mímir v9.2 Web crawler — auto-crawl sub-documents from a URL.

Fetches a page, discovers same-domain links, fetches sub-pages,
extracts content, and returns CollectResults for ingestion.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError


TZ = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (compatible; MimirCrawler/9.2; +https://github.com/mimir-memory/mimir)"
MAX_PAGES = 20
MAX_DEPTH = 2
MAX_CONTENT_LENGTH = 20000


class WebCrawler(BaseCollector):
    """Crawl a URL and discover sub-pages for content ingestion."""

    def __init__(
        self,
        name: str = "crawler",
        enabled: bool = True,
        max_pages: int = MAX_PAGES,
        max_depth: int = MAX_DEPTH,
    ):
        super().__init__(name, enabled)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self._visited: set[str] = set()

    def collect(self) -> list[CollectResult]:
        raise NotImplementedError("Use crawl_url(url) instead")

    def crawl_url(self, url: str, category: str = "knowledge") -> CollectResult:
        """Crawl a URL, discover sub-pages, and return results."""
        if not self.enabled:
            return CollectResult(errors=["crawler disabled"])
        self._visited.clear()
        base_domain = urllib.parse.urlparse(url).netloc
        pages = self._crawl(url, base_domain, depth=0)
        result = CollectResult(
            title=pages[0].get("title", url) if pages else url,
            url=url,
            items_collected=len(pages),
        )
        result._pages = pages
        return result

    def get_pages(self, result: CollectResult) -> list[dict]:
        return getattr(result, "_pages", [])

    def describe(self) -> dict[str, Any]:
        return {
            "type": "crawler",
            "name": self.name,
            "enabled": self.enabled,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
        }

    def _crawl(self, url: str, base_domain: str, depth: int) -> list[dict]:
        if depth > self.max_depth or len(self._visited) >= self.max_pages:
            return []
        if url in self._visited:
            return []
        self._visited.add(url)

        page = self._fetch_page(url)
        if page is None:
            return []

        results = [page]
        if depth < self.max_depth:
            links = self._extract_links(page["html"], url, base_domain)
            for link in links:
                if len(results) >= self.max_pages:
                    break
                results.extend(self._crawl(link, base_domain, depth + 1))
        return results

    def _fetch_page(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return None

        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:200]

        body = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()[:MAX_CONTENT_LENGTH]

        return {
            "title": title,
            "url": url,
            "content": body,
            "source": "web",
            "collected_at": datetime.now(TZ).isoformat(),
            "html": raw,
        }

    @staticmethod
    def _extract_links(html: str, base_url: str, base_domain: str) -> list[str]:
        links = set()
        for m in re.finditer(r'href\s*=\s*["\'](.*?)["\']', html, re.IGNORECASE):
            href = m.group(1).strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            absolute = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(absolute)
            if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
                # Skip anchors, downloads, images
                if any(parsed.path.endswith(ext) for ext in (".pdf", ".zip", ".png", ".jpg", ".gif", ".mp4", ".mp3")):
                    continue
                links.add(absolute.split("#")[0])
        return list(links)
