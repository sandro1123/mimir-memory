"""Mímir v8.2 content collectors package.

Collectors convert external content into ConversationEnvelope for ingestion.
"""

from .base import BaseCollector, CollectorError
from .rss import RSSCollector
from .web import WebCollector
from .crawler import WebCrawler
from .vault import VaultCollector

__all__ = [
    "BaseCollector",
    "CollectorError",
    "RSSCollector",
    "WebCollector",
    "WebCrawler",
    "VaultCollector",
]