"""Mímir source classifier — immutable source category assignment.

This is the single source of truth for mapping connector_type to
source_category. All ingestion and extraction code must use this module.

Categories:
- conversation: hermes_cdc, external_agent, workbuddy
- external_info: rss, web, searxng
- knowledge_doc: feishu, file, document
- unknown/quarantine: any unregistered connector_type
"""

from __future__ import annotations

from typing import Any


# Immutable mapping: connector_type → source_category
SOURCE_CATEGORY_MAP: dict[str, str] = {
    "hermes_cdc": "conversation",
    "external_agent": "conversation",
    "workbuddy": "conversation",
    "rss": "external_info",
    "web": "external_info",
    "searxng": "external_info",
    "feishu": "knowledge_doc",
    "file": "knowledge_doc",
    "document": "knowledge_doc",
}

# Conversation sources (allowed for memory extraction)
CONVERSATION_TYPES = frozenset({"hermes_cdc", "external_agent", "workbuddy"})

# External info sources (not allowed for conversation extraction)
EXTERNAL_INFO_TYPES = frozenset({"rss", "web", "searxng"})

# Knowledge doc sources
KNOWLEDGE_DOC_TYPES = frozenset({"feishu", "file", "document"})

# All registered types
REGISTERED_TYPES = CONVERSATION_TYPES | EXTERNAL_INFO_TYPES | KNOWLEDGE_DOC_TYPES


class UnknownConnectorError(ValueError):
    """Raised when a connector_type is not registered."""


def classify(connector_type: Any) -> str:
    """Return the immutable source_category for a connector_type.
    
    Unregistered connector_types return 'unknown/quarantine'.
    """
    if not isinstance(connector_type, str) or not connector_type.strip():
        return "unknown/quarantine"
    return SOURCE_CATEGORY_MAP.get(connector_type.strip(), "unknown/quarantine")


def is_conversation(connector_type: Any) -> bool:
    """True only for registered conversation sources."""
    if not isinstance(connector_type, str):
        return False
    return connector_type.strip() in CONVERSATION_TYPES


def is_external_info(connector_type: Any) -> bool:
    """True only for registered external info sources."""
    if not isinstance(connector_type, str):
        return False
    return connector_type.strip() in EXTERNAL_INFO_TYPES


def is_knowledge_doc(connector_type: Any) -> bool:
    """True only for registered knowledge document sources."""
    if not isinstance(connector_type, str):
        return False
    return connector_type.strip() in KNOWLEDGE_DOC_TYPES


def is_quarantine(connector_type: Any) -> bool:
    """True for unregistered or invalid connector types."""
    if not isinstance(connector_type, str):
        return True
    return connector_type.strip() not in REGISTERED_TYPES