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


# ── v13.0-3: Proactive & Predictive Recall (intent-driven pre-wake) ──────
#
# The RelevanceGate above is *passive*: an agent asks, the gate decides
# whether retrieval is worth running. The proactive wake inverts the
# direction: on receiving a task brief, Mimir classifies the intent and
# pushes what history says the agent will need — pitfall patterns from
# past incidents and the always-on safety floor of iron rules and core
# user preferences (same semantics as the retrieval anchor channel's
# ANCHOR_FACT_TYPES, duplicated here on purpose: query.py already
# imports this module, so re-importing it would cycle). Pattern matching
# is keyword driven on purpose — light, testable, no LLM dependency.

#: Intent keyword sets. Destructive wins over change (it is the riskier,
#: more specific class); change wins over troubleshooting for the same
#: reason. Order matters — priority, not input position.
INTENT_KEYWORDS = {
    "destructive": (
        "删除", "清空", "销毁", "格式化", "卸载",
        "drop", "truncate", "rm -rf", "purge", "wipe",
    ),
    "change": (
        "改成", "修改", "变更", "更新", "升级", "迁移", "配置成", "重启",
        "update", "upgrade", "migrate", "change", "deploy", "restart",
    ),
    "troubleshooting": (
        "为什么", "排查", "排障", "故障", "报错", "超时", "崩了", "挂了",
        "失败", "异常",
        "crash", "timeout", "error", "fail", "debug", "investigate",
    ),
}

#: Budgets: presence, not unbounded crowding (mirrors the anchor/L2 idea).
WAKE_ANCHOR_BUDGET = 20
WAKE_PATTERN_BUDGET = 10


class IntentProfiler:
    """Keyword-driven intent classification — light, testable, no LLM."""

    @staticmethod
    def classify(text: str) -> str:
        lowered = (text or "").strip().lower()
        for intent in ("destructive", "change", "troubleshooting"):
            for keyword in INTENT_KEYWORDS[intent]:
                if keyword in lowered:
                    return intent
        return "generic"

    @classmethod
    def profile(cls, text: str) -> dict:
        lowered = (text or "").strip().lower()
        matched = sorted({
            keyword
            for keywords in INTENT_KEYWORDS.values()
            for keyword in keywords
            if keyword in lowered
        })
        intent = cls.classify(text)
        return {
            "intent": intent,
            "risky": intent == "destructive",
            "matched_keywords": matched,
        }


class ProactiveWake:
    """Pushes iron rules, user prefs and intent-matched patterns up front.

    The safety floor (iron rules, core user preferences) is pushed for
    every intent — the system's bottom line must never be lost. L2
    patterns ride along only when the task text actually names their
    subject matter; silence beats noise. Every pushed fact is arbitrated
    through CanonicalStore.can_read — the wake never bypasses ACL.
    """

    #: Same semantics as the retrieval anchor channel (query.py
    #: ANCHOR_FACT_TYPES); duplicated here to avoid an import cycle.
    ANCHOR_FACT_TYPES = ("iron_rule", "user_pref")

    def __init__(self, store):
        self.store = store

    def wake(self, text: str, *, principal_id: str, is_admin: bool = False,
             roles: set[str] | None = None) -> dict:
        profile = IntentProfiler.profile(text)
        lowered = (text or "").strip().lower()
        result = {
            "intent": profile["intent"],
            "risky": profile["risky"],
            "profile": profile,
            "iron_rules": [],
            "user_prefs": [],
            "patterns": [],
        }
        # Safety floor first: every iron rule and user pref the principal
        # may read, for any intent.
        for fact_type, bucket in (
            ("iron_rule", "iron_rules"),
            ("user_pref", "user_prefs"),
        ):
            for fact in self._facts_of_type(fact_type)[:WAKE_ANCHOR_BUDGET]:
                if self._can_read(fact, principal_id, is_admin, roles):
                    result[bucket].append(self._memo(fact))
        # L2 patterns: only when the pattern belongs to the same intent
        # family as the task; silence beats noise.
        intent = profile["intent"]
        for fact in self._facts_of_type("pattern"):
            if len(result["patterns"]) >= WAKE_PATTERN_BUDGET:
                break
            content = (fact["content"] or "").lower()
            if content and self._content_matches_intent(
                content, lowered, intent
            ):
                if self._can_read(fact, principal_id, is_admin, roles):
                    result["patterns"].append(self._memo(fact))
        return result

    @classmethod
    def _content_matches_intent(cls, content: str, lowered: str,
                                intent: str) -> bool:
        """A pattern is relevant when it belongs to the same intent family
        as the task: the task text and the pattern content each name the
        intent with their own vocabulary (the task asks "why does the pod
        crash", the pattern says "排障" — both are troubleshooting)."""
        if intent == "generic":
            return False
        task_names_it = any(
            keyword in lowered for keyword in INTENT_KEYWORDS[intent]
        )
        if not task_names_it:
            return False
        return any(
            keyword in content for keyword in INTENT_KEYWORDS[intent]
        )

    def _facts_of_type(self, fact_type: str) -> list[dict]:
        import contextlib

        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                "SELECT fact_id FROM facts WHERE fact_type=? AND status='active'"
                " ORDER BY updated_at DESC",
                (fact_type,),
            ).fetchall()
        facts = []
        for row in rows:
            try:
                facts.append(self.store.get_fact(row["fact_id"]))
            except Exception:
                continue
        return facts

    def _can_read(self, fact, principal_id: str, is_admin: bool,
                  roles: set[str] | None) -> bool:
        try:
            return self.store.can_read(
                fact["fact_id"], principal_id, is_admin=is_admin, roles=roles
            )
        except Exception:
            return False  # Fail-Closed

    @staticmethod
    def _memo(fact: dict) -> dict:
        return {
            "fact_id": fact["fact_id"],
            "content": fact["content"],
            "summary": fact["summary"],
            "fact_type": fact["fact_type"],
            "owner_principal": fact["owner_principal"],
        }
