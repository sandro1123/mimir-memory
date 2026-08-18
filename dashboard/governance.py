"""Mímir v9.1 审核治理管道 — LLM 建议，规则裁决

流程:
  候选 → 确定性规则(去重/噪声/风险) → LLM 评估 → 策略裁决 → 状态更新

模型:
  主力: 通过环境变量 MIMIR_GOVERNANCE_MODEL 配置
  备用: 通过环境变量 MIMIR_GOVERNANCE_FALLBACK_MODEL 配置
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# ── 配置 ──────────────────────────────────────────────
MIMIR_API = os.environ.get("MIMIR_API", "http://127.0.0.1:8456")
ADMIN_TOKEN_FILE = Path.home() / ".hermes/mimir/secrets/clients/admin.token"

# LLM 网关配置（通过环境变量注入）
ROUTER_URL = os.environ.get("MIMIR_ROUTER_URL", "http://127.0.0.1:20128/v1")
ROUTER_API_KEY = os.environ.get("MIMIR_ROUTER_API_KEY", "")

# 主力模型
PRIMARY_MODEL = os.environ.get("MIMIR_GOVERNANCE_MODEL", "default-model")
# 备用模型
FALLBACK_MODEL = os.environ.get("MIMIR_GOVERNANCE_FALLBACK_MODEL", "fallback-model")

# 数据库
CANONICAL_DB = Path(os.environ.get("MIMIR_DATA_DIR", Path.home() / ".hermes/mimir/data")) / "canonical.db"


def save_assessment(assessment: AssessmentResult, candidate_id: str, reviewer_type: str = "rule") -> str:
    """保存评估记录到数据库"""
    assessment_id = str(uuid4())
    try:
        conn = sqlite3.connect(str(CANONICAL_DB))
        conn.execute("""
            INSERT INTO candidate_review_assessments
            (assessment_id, candidate_id, reviewer_type, provider, model, recommendation,
             risk, confidence, is_valuable, is_noise, domain, fact_type, summary, reasoning, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            assessment_id, candidate_id, reviewer_type,
            "llm" if assessment.model_used else None,
            assessment.model_used or None,
            "valuable" if assessment.is_valuable else "noise" if assessment.is_noise else "unknown",
            assessment.risk, assessment.confidence,
            1 if assessment.is_valuable else 0,
            1 if assessment.is_noise else 0,
            assessment.domain, assessment.fact_type,
            assessment.summary, assessment.reasoning[:500],
            1 if assessment.success else 0,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return assessment_id


def save_decision(candidate_id: str, assessment_id: str | None, decision: str, reason: str,
                  previous_status: str, new_status: str, automatic: bool = True) -> str:
    """保存策略裁决到数据库"""
    decision_id = str(uuid4())
    try:
        conn = sqlite3.connect(str(CANONICAL_DB))
        conn.execute("""
            INSERT INTO governance_decisions
            (decision_id, candidate_id, policy_version, assessment_id, decision, reason,
             automatic, actor_principal, previous_status, new_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            decision_id, candidate_id, "v9.1-r1", assessment_id, decision, reason[:500],
            1 if automatic else 0, "service:governance", previous_status, new_status,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return decision_id


# 确定性噪声标记
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

# 高敏感内容 — 必须人工审核
SENSITIVE_PATTERNS = (
    re.compile(r"api.?key|token|secret|password|sk-|pk-", re.IGNORECASE),
    re.compile(r"ssh|private.?key|pem|rsa", re.IGNORECASE),
    re.compile(r"交易|下单|仓位|资金|股票|金额", re.IGNORECASE),
)

# 评估 prompt
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


def get_admin_token() -> str | None:
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


def call_llm(prompt: str, model: str) -> dict | None:
    """调用 LLM 网关"""
    api_key = ROUTER_API_KEY
    if not api_key:
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }

    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            # 提取 JSON
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return None
    except Exception as e:
        return None


def deterministic_check(content: str) -> dict:
    """确定性规则检查"""
    result = {
        "is_noise": False,
        "is_sensitive": False,
        "risk": "low",
        "matched_patterns": [],
    }

    # 噪声检测
    for pattern in NOISE_PATTERNS:
        if pattern.search(content):
            result["is_noise"] = True
            result["matched_patterns"].append(f"noise: {pattern.pattern[:30]}")
            result["risk"] = "low"
            return result  # 命中噪声直接返回

    # 敏感内容检测
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(content):
            result["is_sensitive"] = True
            result["risk"] = "high"
            result["matched_patterns"].append(f"sensitive: {pattern.pattern[:30]}")
            return result

    return result


def assess_candidate(candidate: dict) -> AssessmentResult:
    """评估单个候选"""
    cid = candidate["candidate_id"]
    content = candidate.get("content") or candidate.get("summary", "")

    result = AssessmentResult(candidate_id=cid)

    # 1. 确定性规则检查
    rule_result = deterministic_check(content)

    # 如果确定性规则判定为噪声，直接返回
    if rule_result["is_noise"]:
        result.is_noise = True
        result.risk = "low"
        result.reasoning = f"确定性规则命中噪声: {rule_result['matched_patterns']}"
        result.success = True
        return result

    if rule_result["is_sensitive"]:
        result.risk = "high"
        result.reasoning = f"确定性规则命中敏感内容: {rule_result['matched_patterns']}"
        result.success = True
        return result

    # 2. LLM 评估
    prompt = EVALUATION_PROMPT.format(content=content[:2000])

    # 主力模型
    llm_result = call_llm(prompt, PRIMARY_MODEL)
    model_used = PRIMARY_MODEL

    # 备用模型
    if llm_result is None:
        llm_result = call_llm(prompt, FALLBACK_MODEL)
        model_used = FALLBACK_MODEL

    if llm_result is None:
        result.error = "LLM 不可用（主力+备用均失败）"
        result.risk = "medium"  # 无法评估时保守处理
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
    """策略引擎：根据评估结果做出裁决

    Returns:
        (action, reason)
        action: auto_reject | provisional | human_review
    """
    # 确定性噪声 → 自动拒绝
    if assessment.is_noise:
        return ("auto_reject", "确定性噪声")

    # 敏感内容 → 人工审核
    if assessment.risk in ("high", "critical"):
        return ("human_review", f"高风险({assessment.risk})")

    # LLM 评估失败 → 保守处理
    if not assessment.success:
        return ("human_review", "LLM 评估不可用，需人工确认")

    # LLM 认为无价值 → 自动拒绝（低置信度时转人工）
    if not assessment.is_valuable:
        if assessment.confidence >= 0.8:
            return ("auto_reject", f"LLM 判定无价值(置信度{assessment.confidence:.2f})")
        else:
            return ("human_review", f"LLM 判定无价值但置信度低({assessment.confidence:.2f})")

    # 有价值 + 低风险 → provisional
    if assessment.risk == "low" and assessment.confidence >= 0.7:
        return ("provisional", f"低风险有价值(置信度{assessment.confidence:.2f})")

    # 其他 → 人工审核
    return ("human_review", f"需人工确认(风险={assessment.risk}, 置信度={assessment.confidence:.2f})")


def execute_action(candidate_id: str, action: str, reason: str, assessment: AssessmentResult) -> dict:
    """执行裁决动作"""
    token = get_admin_token()
    if not token:
        return {"error": "no admin token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 映射 action 到 review action
    action_map = {
        "auto_reject": "reject",
        "human_review": None,  # 直接更新数据库
        "provisional": None,   # 直接更新数据库
    }

    mapped_action = action_map.get(action)
    if mapped_action is None:
        # 对于 human_review 和 provisional，直接更新数据库
        new_status = action  # "human_review" 或 "provisional"
        try:
            conn = sqlite3.connect(str(CANONICAL_DB))
            conn.execute(
                "UPDATE candidate_facts SET status=? WHERE candidate_id=? AND status='review_required'",
                (new_status, candidate_id),
            )
            conn.commit()
            conn.close()
            return {"status": "updated", "new_status": new_status, "action": action, "reason": reason}
        except Exception as e:
            return {"error": f"db update failed: {e}"}

    ik = f"governance-{action}-{candidate_id[:8]}-{int(time.time())}"
    payload = {
        "action": mapped_action,
        "reason": f"[v9.1治理] {reason}",
        "idempotency_key": ik,
    }

    try:
        req = urllib.request.Request(
            f"{MIMIR_API}/v8/candidates/{candidate_id}/review",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def run_governance(dry_run: bool = False) -> dict:
    """运行治理管道"""
    token = get_admin_token()
    if not token:
        return {"error": "no admin token"}

    # 1. 获取待审核候选
    headers = {"Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(
            f"{MIMIR_API}/v8/learning/candidates?status=review_required&limit=50",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {"error": f"获取候选失败: {e}"}

    candidates = data.get("candidates", data if isinstance(data, list) else [])
    if not candidates:
        return {"status": "ok", "message": "无待审核候选", "processed": 0}

    results = []
    for candidate in candidates:
        cid = candidate["candidate_id"]
        content = candidate.get("content") or candidate.get("summary", "")

        # 评估
        assessment = assess_candidate(candidate)

        # 裁决
        action, reason = make_decision(assessment)

        # 保存评估记录
        prev_status = candidate.get("status", "review_required")
        new_status = {
            "auto_reject": "auto_rejected",
            "provisional": "provisional",
            "human_review": "human_review",
        }.get(action, prev_status)

        assessment_id = None
        if not dry_run:
            assessment_id = save_assessment(assessment, cid,
                "rule" if not assessment.model_used else "llm")
            save_decision(cid, assessment_id, action, reason, prev_status, new_status)

        entry = {
            "candidate_id": cid,
            "content_preview": content[:80],
            "assessment": {
                "is_noise": assessment.is_noise,
                "is_valuable": assessment.is_valuable,
                "risk": assessment.risk,
                "confidence": round(assessment.confidence, 2),
                "model": assessment.model_used,
                "success": assessment.success,
            },
            "decision": {"action": action, "reason": reason},
        }

        if not dry_run and action in ("auto_reject", "human_review", "provisional"):
            result = execute_action(cid, action, reason, assessment)
            entry["execute_result"] = result

        results.append(entry)

    # 统计
    stats = {}
    for r in results:
        a = r["decision"]["action"]
        stats[a] = stats.get(a, 0) + 1

    return {
        "status": "ok",
        "dry_run": dry_run,
        "processed": len(results),
        "stats": stats,
        "results": results,
    }


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv
    result = run_governance(dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))