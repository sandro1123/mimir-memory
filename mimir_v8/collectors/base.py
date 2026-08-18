"""Base collector class for Mímir v8.2 content ingestion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class CollectorError(RuntimeError):
    """Raised when a collector cannot read or process its source."""


@dataclass
class CollectResult:
    source_id: str | None = None
    title: str = ""
    url: str = ""
    content: str = ""
    items_collected: int = 0
    items_skipped: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class BaseCollector(ABC):
    """Abstract base for all content collectors."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def collect(self) -> list[CollectResult]:
        """Collect content from the source. Returns list of results."""
        ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Return collector metadata for reporting."""
        ...

    @staticmethod
    def truncate(text: str, max_chars: int = 5000) -> str:
        if not text:
            return ""
        return text[:max_chars]