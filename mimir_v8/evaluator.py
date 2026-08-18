"""Mímir v8.2 LLM-powered memory value evaluator.

Strict JSON schema enforcement. Any parse failure produces a parse_error
and the policy layer discards the result. No fallback to "acceptable" default.
"""

from __future__ import annotations

import json
import math
import os
import logging
import re
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

from .schema import DOMAINS, FACT_TYPES, ValidationError

logger = logging.getLogger("mimir_v8.evaluator")


EVALUATOR_VERSION = "v8.2-evaluator-3"

DEFAULT_API_URL = os.environ.get("MIMIR_EVAL_API_URL", "https://api.example.com/v1")
DEFAULT_MODEL = "default-model"
DEFAULT_KEY_ENV = "MIMIR_EVAL_API_KEY"

ALLOWED_RISK = frozenset({"low", "medium", "high", "critical"})
ALLOWED_FACT_TYPES = frozenset(FACT_TYPES)
ALLOWED_DOMAINS = frozenset(DOMAINS)

PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|above|below)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|above|below)", re.IGNORECASE),
    re.compile(r"你不需要\s*(遵守|遵循|按照)", re.IGNORECASE),
    re.compile(r"不需要\s*(遵守|遵循|按照)", re.IGNORECASE),
    re.compile(r"disregard", re.IGNORECASE),
    # Extended (P3): system-prompt override / role hijack / new-instructions /
    # safety bypass. Applied to LLM *output* to catch a hijacked evaluator.
    re.compile(r"(ignore|override|bypass)\s+(the\s+)?(system\s*prompt|safety|rules?|policy)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now|你现在是|从现在起你是", re.IGNORECASE),
    re.compile(r"new\s+instructions?|新的?指令", re.IGNORECASE),
    re.compile(r"忽略(之前|以上|前面|所有)的?(所有)?(指令|规则|提示|设定)", re.IGNORECASE),
    re.compile(r"do\s+not\s+follow|不要(遵守|遵循|执行)(之前|以上|任何)?的?(指令|规则)", re.IGNORECASE),
)

# Layer 1 (P3): input-side prompt-injection guard. Malicious instructions
# embedded in ingested content must be quarantined before they reach the LLM
# evaluator or get persisted as memory. Broader than the output set because a
# hit here only discards one candidate — it never breaks the agent loop.
INPUT_INJECTION_PATTERNS = PROMPT_INJECTION_PATTERNS + (
    re.compile(r"(reveal|show|print|output)\s+(your\s+)?(system\s*prompt|initial\s+prompt|instructions?|初始提示)", re.IGNORECASE),
    re.compile(r"(泄露|告诉我|输出)(你的)?(系统|初始)(提示词|指令|prompt)", re.IGNORECASE),
    re.compile(r"jailbreak|越狱", re.IGNORECASE),
    re.compile(r"\bDAN\b|developer\s+mode\s+(enabled|on)", re.IGNORECASE),
    # Extended (P1-5): broader input-side coverage. A hit here only discards
    # one candidate, so false-positive cost stays low while recall improves.
    # fake system/role framing
    re.compile(r"system\s*:\s*you\s+are", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>|\[\s*system\s*\]", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)|act\s+as\s+(if|though)", re.IGNORECASE),
    re.compile(r"假装|扮演|你现在扮演", re.IGNORECASE),
    # privilege escalation / command execution
    re.compile(r"(admin|sudo|root)\s+mode|elevate\s+(your\s+)?privileges?", re.IGNORECASE),
    re.compile(r"(execute|run)\s+(this\s+)?(shell|bash|system|terminal)\s+command", re.IGNORECASE),
    re.compile(r"(rm\s+-rf|format\s+c:|drop\s+table|delete\s+all\s+(files|data))", re.IGNORECASE),
    re.compile(r"(curl|wget)\s+[^\s]+\s*\|\s*(ba)?sh", re.IGNORECASE),
    # data exfiltration
    re.compile(r"(exfiltrate|send|upload|post)\s+(this\s+|the\s+|all\s+)?(conversation|chat|data|memor(?:y|ies)|facts?)(\s+\w+)?\s+to", re.IGNORECASE),
    re.compile(r"(把|将)(对话|聊天|数据|记忆)(发送|上传|转发)到", re.IGNORECASE),
    # credential probing (directed at the agent, not generic mentions)
    re.compile(r"what\s+(is|are)\s+your\s+(api[_\s-]?key|secret|password|credentials)", re.IGNORECASE),
    re.compile(r"(你的|告诉我)(api[_\s-]?key|密钥|密码|凭证)(是什么|多少)?", re.IGNORECASE),
    # obfuscation / known jailbreak framings
    re.compile(r"base64\s*:\s*[A-Za-z0-9+/=]{16,}", re.IGNORECASE),
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"无(限制|约束)模式|开发者模式", re.IGNORECASE),
    re.compile(r"(bypass|disable|turn\s+off)\s+(your\s+)?(safety|filter|guard|content\s*policy)", re.IGNORECASE),
    re.compile(r"绕过(安全|审查|过滤|限制)", re.IGNORECASE),
)

# Strict JSON object pattern: must be a single complete JSON object
# with no surrounding text
_STRICT_JSON_OBJECT = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)


@dataclass(frozen=True)
class EvaluationResult:
    content: str
    salience: float
    risk: str
    domain: str
    fact_type: str
    summary: str
    reasoning: str
    is_valuable: bool
    policy_version: str = EVALUATOR_VERSION
    model_used: str = ""
    raw_response: str = ""
    parse_error: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    evaluation: EvaluationResult


class Evaluator:
    """Strict JSON schema evaluator. Any parse failure → parse_error."""

    def __init__(
        self,
        api_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        salience_drop: float = 0.3,
        salience_fast_track: float = 0.7,
        fallback_markers: tuple[str, ...] = ("我偏好", "我希望", "请记住", "以后请", "必须", "不要"),
    ):
        self.api_url = (api_url or os.environ.get("MIMIR_EVALUATOR_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.model = model or os.environ.get("MIMIR_EVALUATOR_MODEL") or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("MIMIR_EVALUATOR_API_KEY") or ""
        self._key_explicitly_empty = api_key is not None and api_key == ""
        self.salience_drop = salience_drop
        self.salience_fast_track = salience_fast_track
        self.fallback_markers = fallback_markers

    def evaluate(self, content: str) -> EvaluationResult:
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("content must be non-empty text")
        if len(content) > 10000:
            content = content[:10000]

        # Layer 1 (P3): input-side prompt-injection guard. Quarantine and
        # discard before the content reaches the LLM or is stored.
        for pattern in INPUT_INJECTION_PATTERNS:
            if pattern.search(content):
                self._quarantine_log("input", content, pattern.pattern)
                logger.warning("input injection pattern detected: %s", pattern.pattern)
                return EvaluationResult(
                    content=content, salience=0.0, risk="critical",
                    domain="knowledge", fact_type="reference",
                    summary="", reasoning="",
                    is_valuable=False, raw_response="",
                    parse_error=f"input injection pattern detected: {pattern.pattern}",
                )

        if not self.api_key or self._key_explicitly_empty:
            logger.warning("evaluator api_key missing; degrading to rule-based fallback (set MIMIR_EVALUATOR_API_KEY)")
            return self._fallback_rule_based(content)

        prompt = self._build_prompt(content)
        raw = self._call_llm(prompt)
        return self._parse_response(raw, content)

    def _quarantine_log(self, layer: str, content: str, pattern: str) -> None:
        """Append a quarantine record (layer 3) so detected injections are auditable."""
        try:
            import time
            log_dir = os.environ.get("MIMIR_LOG_DIR", "")
            if not log_dir:
                return
            from pathlib import Path as _Path
            path = _Path(log_dir) / "injection_quarantine.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "layer": layer,
                "pattern": pattern,
                "content_preview": content[:300],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def decide(self, evaluation: EvaluationResult) -> PolicyDecision:
        if evaluation.parse_error:
            return PolicyDecision(
                action="discard",
                reason=f"strict parse error: {evaluation.parse_error}",
                evaluation=evaluation,
            )
        if evaluation.risk == "critical":
            return PolicyDecision(
                action="reject",
                reason="critical risk content rejected by policy",
                evaluation=evaluation,
            )
        if not evaluation.is_valuable or evaluation.salience < self.salience_drop:
            return PolicyDecision(
                action="discard",
                reason=f"salience {evaluation.salience} below drop threshold {self.salience_drop}",
                evaluation=evaluation,
            )
        if evaluation.risk == "high" or evaluation.fact_type in ("iron_rule",) or evaluation.domain == "personal":
            return PolicyDecision(
                action="strict_review",
                reason="high risk or personal/iron_rule requires human review",
                evaluation=evaluation,
            )
        if evaluation.risk == "low" and evaluation.salience >= self.salience_fast_track:
            return PolicyDecision(
                action="fast_track",
                reason=f"low risk with high salience ({evaluation.salience}) qualifies for fast track",
                evaluation=evaluation,
            )
        return PolicyDecision(
            action="standard_review",
            reason="standard review required",
            evaluation=evaluation,
        )

    def _build_prompt(self, content: str) -> list[dict]:
        return [
            {
                "role": "system",
                "content": (
                    "你是一位记忆价值评估专家。分析以下对话或内容，判断是否值得作为AI Agent的长期记忆保存。\n\n"
                    "评估标准：\n"
                    "1. 是否包含稳定的用户偏好、规则、决策或已验证的事实\n"
                    "2. 是否可跨会话复用\n"
                    "3. 是否是临时/一次性/闲聊内容\n"
                    "4. 是否包含敏感信息（密码、token、密钥等）\n\n"
                    "请以JSON格式输出，严格遵循以下schema：\n"
                    "{\n"
                    '  "is_valuable": true,\n'
                    '  "salience": 0.8,\n'
                    '  "risk": "low",\n'
                    '  "domain": "personal",\n'
                    '  "fact_type": "user_pref",\n'
                    '  "summary": "一句话摘要",\n'
                    '  "reasoning": "为什么认为值得或不值得记忆"\n'
                    "}\n\n"
                    "重要规则：\n"
                    "- is_valuable 必须是 JSON 布尔值 true 或 false\n"
                    "- salience 必须是 0.0 到 1.0 之间的数字\n"
                    "- risk 只能是 low/medium/high/critical 之一\n"
                    "- domain 只能是 infrastructure/quant/tech_support/personal/system/knowledge 之一\n"
                    "- fact_type 只能是 iron_rule/user_pref/project_config/event/pattern/learning/reference 之一\n"
                    "- summary 和 reasoning 必须是字符串\n"
                    "- 不要输出除 JSON 以外的任何内容\n"
                    "- 不要添加额外字段\n"
                    "只输出JSON，不要其他文字。"
                ),
            },
            {"role": "user", "content": f"请评估以下内容：\n\n{content}"},
        ]

    def _call_llm(self, messages: list[dict]) -> str | None:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 512,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            choices = result.get("choices", [])
            if not choices:
                return None
            return choices[0].get("message", {}).get("content", "")
        except Exception as exc:
            logger.warning("evaluator LLM call failed: %s: %s", type(exc).__name__, exc)
            return None

    def _parse_response(self, raw: str | None, content: str) -> EvaluationResult:
        if not raw:
            return EvaluationResult(
                content=content, salience=0.0, risk="low",
                domain="knowledge", fact_type="reference",
                summary="", reasoning="",
                is_valuable=False, raw_response="",
                model_used=self.model,
                parse_error="LLM returned empty response",
            )

        # Must be a single complete JSON object with no surrounding text
        if not _STRICT_JSON_OBJECT.match(raw):
            return EvaluationResult(
                content=content, salience=0.0, risk="low",
                domain="knowledge", fact_type="reference",
                summary="", reasoning="",
                is_valuable=False, raw_response=raw,
                model_used=self.model,
                parse_error="response is not a single JSON object (surrounding text detected)",
            )

        try:
            data = json.loads(
                raw,
                parse_constant=self._reject_constant,
                object_pairs_hook=self._reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError) as e:
            return EvaluationResult(
                content=content, salience=0.0, risk="low",
                domain="knowledge", fact_type="reference",
                summary="", reasoning="",
                is_valuable=False, raw_response=raw,
                model_used=self.model,
                parse_error=f"JSON decode error: {e}",
            )

        if not isinstance(data, dict):
            return EvaluationResult(
                content=content, salience=0.0, risk="low",
                domain="knowledge", fact_type="reference",
                summary="", reasoning="",
                is_valuable=False, raw_response=raw,
                model_used=self.model,
                parse_error=f"JSON root is {type(data).__name__}, expected object",
            )

        # Check for prompt injection
        raw_text = raw
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(raw_text):
                return EvaluationResult(
                    content=content, salience=0.0, risk="low",
                    domain="knowledge", fact_type="reference",
                    summary="", reasoning="",
                    is_valuable=False, raw_response=raw,
                    model_used=self.model,
                    parse_error=f"prompt injection pattern detected: {pattern.pattern}",
                )

        # Check for extra fields
        allowed_fields = {"is_valuable", "salience", "risk", "domain",
                          "fact_type", "summary", "reasoning"}
        extra = set(data.keys()) - allowed_fields
        if extra:
            return EvaluationResult(
                content=content, salience=0.0, risk="low",
                domain="knowledge", fact_type="reference",
                summary="", reasoning="",
                is_valuable=False, raw_response=raw,
                model_used=self.model,
                parse_error=f"extra fields not allowed: {sorted(extra)}",
            )

        # --- is_valuable: must be bool ---
        is_valuable_raw = data.get("is_valuable")
        if is_valuable_raw is None:
            return self._parse_error(raw, content, "is_valuable is required")
        if not isinstance(is_valuable_raw, bool):
            return self._parse_error(raw, content, f"is_valuable must be JSON bool, got {type(is_valuable_raw).__name__}: {is_valuable_raw!r}")

        # --- salience: must be int/float (not bool), finite, in [0,1] ---
        salience_raw = data.get("salience")
        if salience_raw is None:
            return self._parse_error(raw, content, "salience is required")
        if isinstance(salience_raw, bool):
            return self._parse_error(raw, content, f"salience must be a number, got bool: {salience_raw!r}")
        if not isinstance(salience_raw, (int, float)):
            return self._parse_error(raw, content, f"salience must be a number, got {type(salience_raw).__name__}: {salience_raw!r}")
        salience = float(salience_raw)
        if not math.isfinite(salience):
            return self._parse_error(raw, content, f"salience must be finite, got {salience}")
        if salience < 0.0 or salience > 1.0:
            return self._parse_error(raw, content, f"salience {salience} out of range [0.0, 1.0]")

        # --- risk: must be string in whitelist ---
        risk_raw = data.get("risk")
        if risk_raw is None:
            return self._parse_error(raw, content, "risk is required")
        if not isinstance(risk_raw, str):
            return self._parse_error(raw, content, f"risk must be a string, got {type(risk_raw).__name__}: {risk_raw!r}")
        risk = risk_raw.strip().lower()
        if risk not in ALLOWED_RISK:
            return self._parse_error(raw, content, f"risk {risk!r} not in {sorted(ALLOWED_RISK)}")

        # --- domain: must be string in whitelist ---
        domain_raw = data.get("domain")
        if domain_raw is None:
            return self._parse_error(raw, content, "domain is required")
        if not isinstance(domain_raw, str):
            return self._parse_error(raw, content, f"domain must be a string, got {type(domain_raw).__name__}: {domain_raw!r}")
        domain = domain_raw.strip().lower()
        if domain not in ALLOWED_DOMAINS:
            return self._parse_error(raw, content, f"domain {domain!r} not in {sorted(ALLOWED_DOMAINS)}")

        # --- fact_type: must be string in whitelist ---
        fact_type_raw = data.get("fact_type")
        if fact_type_raw is None:
            return self._parse_error(raw, content, "fact_type is required")
        if not isinstance(fact_type_raw, str):
            return self._parse_error(raw, content, f"fact_type must be a string, got {type(fact_type_raw).__name__}: {fact_type_raw!r}")
        fact_type = fact_type_raw.strip().lower()
        if fact_type not in ALLOWED_FACT_TYPES:
            return self._parse_error(raw, content, f"fact_type {fact_type!r} not in {sorted(ALLOWED_FACT_TYPES)}")

        # --- summary: must be string ---
        summary_raw = data.get("summary")
        if summary_raw is None:
            return self._parse_error(raw, content, "summary is required")
        if not isinstance(summary_raw, str):
            return self._parse_error(raw, content, f"summary must be a string, got {type(summary_raw).__name__}: {summary_raw!r}")

        # --- reasoning: must be string ---
        reasoning_raw = data.get("reasoning")
        if reasoning_raw is None:
            return self._parse_error(raw, content, "reasoning is required")
        if not isinstance(reasoning_raw, str):
            return self._parse_error(raw, content, f"reasoning must be a string, got {type(reasoning_raw).__name__}: {reasoning_raw!r}")

        return EvaluationResult(
            content=content,
            salience=salience,
            risk=risk,
            domain=domain,
            fact_type=fact_type,
            summary=summary_raw[:200],
            reasoning=reasoning_raw,
            is_valuable=is_valuable_raw,
            raw_response=raw,
            model_used=self.model,
        )

    def _parse_error(self, raw: str, content: str, msg: str) -> EvaluationResult:
        return EvaluationResult(
            content=content, salience=0.0, risk="low",
            domain="knowledge", fact_type="reference",
            summary="", reasoning="",
            is_valuable=False, raw_response=raw or "",
            model_used=self.model,
            parse_error=msg,
        )

    @staticmethod
    def _reject_constant(constant: str) -> float:
        raise ValueError(f"JSON constant not allowed: {constant}")

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key not allowed: {key}")
            result[key] = value
        return result

    def _fallback_rule_based(self, content: str) -> EvaluationResult:
        for marker in self.fallback_markers:
            if marker in content:
                return EvaluationResult(
                    content=content,
                    salience=0.6,
                    risk="medium",
                    domain="personal",
                    fact_type="user_pref",
                    summary=content[:100],
                    reasoning=f"keyword match: '{marker}' (LLM unavailable, rule fallback)",
                    is_valuable=True,
                    policy_version=f"{EVALUATOR_VERSION}-fallback",
                )
        return EvaluationResult(
            content=content,
            salience=0.0,
            risk="low",
            domain="knowledge",
            fact_type="reference",
            summary="",
            reasoning="no valuable pattern detected (rule fallback)",
            is_valuable=False,
            policy_version=f"{EVALUATOR_VERSION}-fallback",
        )