"""Mímir v9.2 Relevance Gate — heuristic pre-search check.

Determines whether a query needs memory retrieval or can skip it.
Reduces unnecessary LLM/vector calls for chit-chat and non-memory queries.
"""

from __future__ import annotations

import re
from typing import Any

# Patterns that strongly indicate memory retrieval is needed
MEMORY_KEYWORDS = re.compile(
    r"(记住|记得|之前|上次|以前|说过|讲过|提到过|写过|看过|"
    r"叫什么|是什么|在哪里|什么时候|怎么回事|"
    r"记不记得|有没有|找一下|查一下|搜一下|回忆|"
    r"remember|recall|previous|before|last time|what.*(name|is|was)|"
    r"where (is|are|was|were)|who (is|was)|"
    r"find|search|look up|check|tell me about)",
    re.IGNORECASE,
)

# Generic domain keywords and technical concepts that indicate context is needed
ENTITY_PATTERNS = re.compile(
    r"\b(agent|assistant|memory|chromadb|sqlite|database|"
    r"config|token|api|endpoint|server|service|system|"
    r"architecture|workflow|pipeline|docker|kubernetes)\b",
    re.IGNORECASE,
)

# Question patterns that need context
QUESTION_PATTERNS = re.compile(
    r"^(什么|怎么|为什么|如何|哪个|哪里|何时|多少|"
    r"what|how|why|which|where|when|who|whose|"
    r"can|could|would|should|is|are|do|does|did|has|have)",
    re.IGNORECASE,
)

# Greeting/chit-chat — skip retrieval
CHITCHAT_PATTERNS = re.compile(
    r"^(你好|嗨|哈[喽罗]|早上好|下午好|晚上好|"
    r"hello|hi|hey|good morning|good afternoon|good evening|"
    r"谢谢|thank|thanks|好的|ok|嗯|好的吧|可以|"
    r"再见|拜拜|bye|goodbye|晚安|good night)",
    re.IGNORECASE,
)

# Short queries that are unlikely to need memory
SHORT_QUERY_MAX_LENGTH = 3


class RelevanceGate:
    """Heuristic gate that decides whether to run memory retrieval."""

    @staticmethod
    def should_search(query: str) -> tuple[bool, str]:
        """Returns (should_search, reason).

        Returns True if the query likely needs memory context.
        Returns False if the query can skip retrieval (chit-chat, greeting, etc.).
        """
        if not query or not isinstance(query, str):
            return False, "empty query"

        stripped = query.strip()
        if not stripped:
            return False, "blank query"

        # Very short queries are unlikely to need memory
        if len(stripped) < SHORT_QUERY_MAX_LENGTH:
            return False, f"too short ({len(stripped)} chars)"

        # Chit-chat/greeting — skip
        if CHITCHAT_PATTERNS.match(stripped):
            return False, "chit-chat pattern matched"

        # Memory keywords — definitely search
        if MEMORY_KEYWORDS.search(stripped):
            return True, "memory keyword matched"

        # Entity names — search
        if ENTITY_PATTERNS.search(stripped):
            return True, "entity name matched"

        # Question patterns — search
        if QUESTION_PATTERNS.match(stripped):
            return True, "question pattern matched"

        # Longer queries that contain Chinese characters likely need memory
        chinese_chars = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff')
        if chinese_chars >= 3:
            return True, "Chinese content ≥3 chars"

        # Default: not confident enough to block retrieval
        return True, "default — search allowed"
