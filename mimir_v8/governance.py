"""Governance pipeline for Mímir v10 — LLM evaluation, rule-based decisions, auto-commit.

This replaces the standalone dashboard/governance.py by living inside the mimir_v8
package and using the API (with admin token) for all status changes, so audit_log
and event sourcing are preserved.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .store import CanonicalStore
from .candidates import CandidateService, ReviewCandidate
from .schema import MIMIR_VERSION


# ── Config ──────────────────────────────────────────────
ROUTER_URL = os.environ.get("MIMIR_ROUTER_URL", "http://127.0.0.1:20128/v1")
ROUTER_API_KEY = os.environ.get("MIMIR_ROUTER_API_KEY", "")
PRIMARY_MODEL = os.environ.get("MIMIR_GOVERNANCE_MODEL", "default-model")
FALLBACK_MODEL = os.environ.get("MIMIR_GOVERNANCE_FALLBACK_MODEL", "fallback-model")
GOVERNANCE_AUTO_APPROVE = os.environ.get("MIMIR_GOVERNANCE_AUTO_APPROVE", "1") == "1"
GOVERNANCE_FAST_TRACK_THRESHOLD = float(os.environ.get("MIMIR_FAST_TRACK_THRESHOLD", "0.8"))


# Noise patterns
NOISE_PATTERNS = (
    re.compile(r"\[CONTEXT COMPACTION[^\]]*\]", re.IGNORECASE),
    re.compile(r"\[IMPORTANT:.*?(cron job|skill|DELIVERY)", re.IGNORECASE),
    re.compile(r"^The user (sent|invoked)", re.IGNORECASE),
    re.compile(r"^Replying to:", re.IGNORECASE),
    re.compile(r"Earlier turns were compacted", re.IGNORECASE),
    re.compile(r"^This is a handoff from", re.IGNORECASE),
    re.compile(r"^You are running as a scheduled", re.IGNORECASE),
    re.compile(r"^# ✅ 全流程验证", re.IGNORECASE),
)
SENSITIVE_PATTERNS = (
    re.compile(r"api.?key|token|secret|password|sk-|pk-", re.IGNORECASE),
    re.compile(r"ssh|private.?key|pem|rsa", re.IGNORECASE),
    re.compile(r"交易|下单|仓位|资金|股票|金额", re.IGNORECASE),
)

EVALUATION_PROMPT = """你是一个记忆质量评估器。判断以下候选内容是否适合写入 Mímir 联邦记忆系统。

候选内容：
{content}

请输出严格的 JSON（不要任何其他文字）：
```json
{{
  "is_valuable": true/false,
  "is_noise": true/false,
  "risk": "low|medium|high|critical",
  "domain": "system|infrastructure|personal|quant|tech_support",
  "fact_type": "iron_rule|user_pref|project_config|event|pattern|learning|reference",
  "summary": "一句话摘要（中文）",
  "confidence": 0.0-1.0,
  "reasoning": "判断理由（中文）"
}}
```

判断标准：
- is_valuable=true: 长期稳定的知识、偏好、配置、规则
- is_noise=true: 临时任务指令、上下文压缩、系统提示、执行日志
- risk=high/critical: 涉及 API Key、密码、交易、权限变更
- confidence: 你对自己判断的确信度

注意：用户明确表达的需求、偏好、决策应该标记为 valuable。"""


@dataclass
class AssessmentResult:
    candidate_id: str
    is_noise: bool = False
    is_valuable: bool = False
    risk: str = "medium"
    domain: str = "personal"
    fact_type: str = "user_pref"
    summary: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    model_used: str = ""
    success: bool = False
    error: str = ""


def _call_llm(prompt: str, model: str) -> dict | None:
    api_key = ROUTER_API_KEY
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 1024}
    try:
        req = urllib.request.Request(f"{ROUTER_URL}/chat/completions", data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
            match = re.search(r"\{.*\}", content, re.DOTALL)
            return json.loads(match.group()) if match else None
    except Exception:
        return None


def deterministic_check(content: str) -> dict:
    result = {"is_noise": False, "is_sensitive": False, "risk": "low", "matched_patterns": []}
    for p in NOISE_PATTERNS:
        if p.search(content):
            result["is_noise"] = True
            result["matched_patterns"].append(f"noise: {p.pattern[:30]}")
            return result
    for p in SENSITIVE_PATTERNS:
        if p.search(content):
            result["is_sensitive"] = True
            result["risk"] = "high"
            result["matched_patterns"].append(f"sensitive: {p.pattern[:30]}")
            return result
    return result


def assess_candidate(content: str, candidate_id: str) -> AssessmentResult:
    result = AssessmentResult(candidate_id=candidate_id)
    rule = deterministic_check(content)
    if rule["is_noise"]:
        result.is_noise = True
        result.risk = "low"
        result.reasoning = f"确定性规则命中噪声: {rule['matched_patterns']}"
        result.success = True
        return result
    if rule["is_sensitive"]:
        result.risk = "high"
        result.reasoning = f"确定性规则命中敏感内容: {rule['matched_patterns']}"
        result.success = True
        return result
    prompt = EVALUATION_PROMPT.format(content=content[:2000])
    llm_result = _call_llm(prompt, PRIMARY_MODEL)
    model_used = PRIMARY_MODEL
    if llm_result is None:
        llm_result = _call_llm(prompt, FALLBACK_MODEL)
        model_used = FALLBACK_MODEL
    if llm_result is None:
        result.error = "LLM 不可用"
        result.risk = "medium"
        return result
    result.model_used = model_used
    result.is_valuable = llm_result.get("is_valuable", False)
    result.is_noise = llm_result.get("is_noise", False)
    result.risk = llm_result.get("risk", "medium")
    result.domain = llm_result.get("domain", "personal")
    result.fact_type = llm_result.get("fact_type", "user_pref")
    result.summary = llm_result.get("summary", "")[:200]
    result.confidence = float(llm_result.get("confidence", 0.5))
    result.reasoning = llm_result.get("reasoning", "")
    result.success = True
    return result


def make_decision(assessment: AssessmentResult) -> tuple[str, str]:
    if assessment.is_noise:
        return ("auto_reject", "确定性噪声")
    if assessment.risk in ("high", "critical"):
        return ("human_review", f"高风险({assessment.risk})")
    if not assessment.success:
        return ("human_review", "LLM 评估不可用，需人工确认")
    if not assessment.is_valuable:
        if assessment.confidence >= 0.8:
            return ("auto_reject", f"LLM 判定无价值(置信度{assessment.confidence:.2f})")
        return ("human_review", f"LLM 判定无价值但置信度低({assessment.confidence:.2f})")
    if assessment.risk == "low" and assessment.confidence >= 0.7:
        return ("provisional", f"低风险有价值(置信度{assessment.confidence:.2f})")
    return ("human_review", f"需人工确认(风险={assessment.risk}, 置信度={assessment.confidence:.2f})")


def run_governance_once(store: CanonicalStore, candidate_service: CandidateService, *, dry_run: bool = False, actor: str = "service:governance") -> dict:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT candidate_id, content, summary, status FROM candidate_facts WHERE status='review_required' ORDER BY created_at ASC LIMIT 50"
        ).fetchall()
    if not rows:
        return {"status": "ok", "message": "无待审核候选", "processed": 0, "results": []}

    results = []
    for row in rows:
        cid = row["candidate_id"]
        content = row["content"] or row["summary"] or ""
        assessment = assess_candidate(content, cid)
        action, reason = make_decision(assessment)

        entry = {
            "candidate_id": cid,
            "content_preview": content[:100],
            "assessment": {
                "is_noise": assessment.is_noise, "is_valuable": assessment.is_valuable,
                "risk": assessment.risk, "confidence": round(assessment.confidence, 2),
                "model": assessment.model_used, "success": assessment.success,
            },
            "decision": {"action": action, "reason": reason},
            "executed": False,
        }

        if not dry_run:
            try:
                # P0-2 loop-closure fix: persist the assessment and the decision
                # so the dashboard/audit trail show WHY a candidate landed where
                # it did. Previously these were computed and thrown away.
                assessment_id = str(uuid4())
                decision_now = utc_now()
                new_status = {"auto_reject": "auto_rejected", "provisional": "provisional", "human_review": "human_review"}[action]
                with store.transaction() as conn:
                    conn.execute(
                        """INSERT INTO candidate_review_assessments(
                            assessment_id, candidate_id, reviewer_type, provider, model,
                            recommendation, risk, confidence, is_valuable, is_noise,
                            domain, fact_type, summary, reasoning, raw_output_hash,
                            success, error_code, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (assessment_id, cid, "llm", "router", assessment.model_used,
                         "valuable" if assessment.is_valuable else ("noise" if assessment.is_noise else "unknown"),
                         assessment.risk, round(assessment.confidence, 2),
                         int(assessment.is_valuable), int(assessment.is_noise),
                         assessment.domain, assessment.fact_type, assessment.summary,
                         assessment.reasoning, "", int(assessment.success),
                         assessment.error or None, decision_now),
                    )
                    conn.execute(
                        """INSERT INTO governance_decisions(
                            decision_id, candidate_id, policy_version, assessment_id,
                            decision, reason, automatic, actor_principal,
                            previous_status, new_status, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid4()), cid, "v10-r2", assessment_id, action, reason,
                         1, actor, "review_required", new_status, decision_now),
                    )
                entry["assessment_id"] = assessment_id
                if action == "auto_reject":
                    candidate_service.review_candidate(
                        ReviewCandidate(candidate_id=cid, action="reject", reason=f"[v10治理] {reason}", idempotency_key=f"gov-reject-{cid}-{utc_now()}"),
                        actor,
                    )
                    entry["executed"] = True
                elif action == "provisional":
                    with store.transaction() as conn:
                        conn.execute("UPDATE candidate_facts SET status='provisional' WHERE candidate_id=? AND status='review_required'", (cid,))
                    entry["executed"] = True
                elif action == "human_review":
                    with store.transaction() as conn:
                        conn.execute("UPDATE candidate_facts SET status='human_review' WHERE candidate_id=? AND status='review_required'", (cid,))
                    entry["executed"] = True
            except Exception as e:
                entry["error"] = str(e)
        results.append(entry)

    stats: dict[str, int] = {}
    for r in results:
        a = r["decision"]["action"]
        stats[a] = stats.get(a, 0) + 1
    return {"status": "ok", "dry_run": dry_run, "processed": len(results), "stats": stats, "results": results}


def fast_track_commit_all(store: CanonicalStore, candidate_service: CandidateService, *, actor: str = "service:governance") -> dict:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT candidate_id, content, summary, confidence_score, created_at FROM candidate_facts WHERE status='provisional' ORDER BY created_at ASC LIMIT 20"
        ).fetchall()
    if not rows:
        return {"status": "ok", "committed": 0, "message": "无 provisional 候选可自动提交"}
    committed = 0
    errors = []
    for row in rows:
        cid = row["candidate_id"]
        if (row["confidence_score"] or 0) < GOVERNANCE_FAST_TRACK_THRESHOLD:
            content = row["content"] or row["summary"] or ""
            assessment = assess_candidate(content, cid)
            if assessment.confidence < GOVERNANCE_FAST_TRACK_THRESHOLD:
                errors.append({"candidate_id": cid, "error": "confidence below threshold"})
                continue
        try:
            candidate_service.review_candidate(
                ReviewCandidate(candidate_id=cid, action="approve", reason="[v10治理] fast_track 自动审批", idempotency_key=f"gov-approve-{cid}-{utc_now()}"),
                actor,
            )
            candidate_service.commit_approved(cid, idempotency_key=f"gov-commit-{cid}-{utc_now()}", actor_principal=actor)
            committed += 1
        except Exception as e:
            errors.append({"candidate_id": cid, "error": str(e)})
    return {"status": "ok", "committed": committed, "errors": errors}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()