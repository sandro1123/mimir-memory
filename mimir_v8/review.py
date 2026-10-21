"""Mímir v8.2 review queue management and reminder system.

Handles pending candidate review queue, priority sorting,
and generates notifications for the Feishu reminder pipeline.
"""

from __future__ import annotations

import json
import math
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from .store import CanonicalStore


TZ = timezone(timedelta(hours=8))
RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# issue #13：待审老化分级（小时）。13 小时与 13 天在单一数字里几乎没区别，
# 分级让告警自己说得出严重程度，而不是等人去读那个数。
AGING_HOURS = 24.0    # 超过一天：开始引人注意，但还不到喊人的程度
STALE_HOURS = 72.0    # 超过三天：该处理了
AGE_BUCKETS = ("fresh", "aging", "stale")


def age_bucket(wait_hours: float) -> str:
    """把等待时长映射为分级：fresh / aging / stale。

    边界取「含下界」：恰好 24h 即 aging，恰好 72h 即 stale。
    """
    if isinstance(wait_hours, bool) or not isinstance(wait_hours, (int, float)):
        raise ValueError("wait_hours must be a number")
    if wait_hours != wait_hours or wait_hours in (float("inf"), float("-inf")):
        raise ValueError("wait_hours must be finite")
    if wait_hours < 0:
        raise ValueError("wait_hours must not be negative")
    if wait_hours >= STALE_HOURS:
        return "stale"
    if wait_hours >= AGING_HOURS:
        return "aging"
    return "fresh"


class UncertaintyParseError(ValueError):
    """Raised when uncertainty_json cannot be parsed as a valid type."""


@dataclass
class ReviewItem:
    candidate_id: str
    content: str
    summary: str
    proposed_owner: str
    proposed_domain: str
    proposed_fact_type: str
    risk: str
    salience: float
    source_kind: str
    created_at: str
    wait_hours: float
    evidence_count: int
    uncertainty_error: str = ""
    # issue #13：状态必须逐条暴露。human_review 是"等人工拍板"，
    # review_required 是"等评估"，两者混在一个列表里会让审阅人分不清
    # 哪条真在等自己。
    status: str = "review_required"


@dataclass
class ReviewQueueSummary:
    total: int
    by_risk: dict[str, int]
    by_domain: dict[str, int]
    by_owner: dict[str, int]
    oldest_wait_hours: float
    items: list[ReviewItem] = field(default_factory=list)
    parse_errors: list[dict] = field(default_factory=list)


class ReviewQueue:
    """Review queue management for pending candidates."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def list_pending(
        self,
        owner: str | None = None,
        domain: str | None = None,
        risk: str | None = None,
        limit: int = 50,
    ) -> list[ReviewItem]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("review queue limit must be an integer between 1 and 500")
        if risk is not None and risk not in RISK_ORDER:
            raise ValueError(f"invalid review risk: {risk}")
        items = []
        now = datetime.now(TZ)

        with closing(self.store.connect()) as connection:
            query = """
                SELECT c.candidate_id, c.content, c.summary,
                       c.proposed_owner_principal, c.proposed_domain,
                       c.proposed_fact_type, c.uncertainty_json,
                       c.created_at, c.status
                FROM candidate_facts c
                WHERE c.status IN ('review_required', 'human_review')
            """
            params: list[Any] = []
            if owner:
                query += " AND c.proposed_owner_principal = ?"
                params.append(owner)
            if domain:
                query += " AND c.proposed_domain = ?"
                params.append(domain)

            query += " ORDER BY c.created_at ASC LIMIT ?"
            params.append(limit)

            rows = connection.execute(query, params).fetchall()

            for row in rows:
                uncertainty_error = ""
                try:
                    created = self._parse_created_at(row["created_at"])
                except ValueError as exc:
                    created = now
                    uncertainty_error = str(exc)
                wait = max(0.0, (now - created).total_seconds() / 3600)

                try:
                    uncertainty = self._parse_uncertainty(row["uncertainty_json"])
                except UncertaintyParseError as exc:
                    uncertainty = {}
                    uncertainty_error = self._join_errors(uncertainty_error, str(exc))

                item_risk = self._infer_risk(
                    row["proposed_fact_type"], row["proposed_domain"], uncertainty
                )
                if risk is not None and item_risk != risk:
                    continue
                salience = self._infer_salience(uncertainty)

                evidence_count = 0
                ev_row = connection.execute(
                    "SELECT COUNT(*) AS cnt FROM candidate_evidence WHERE candidate_id=?",
                    (row["candidate_id"],),
                ).fetchone()
                if ev_row:
                    evidence_count = ev_row["cnt"]

                items.append(ReviewItem(
                    candidate_id=row["candidate_id"],
                    content=str(row["content"] or "")[:200],
                    summary=str(row["summary"] or "")[:200],
                    proposed_owner=str(row["proposed_owner_principal"] or ""),
                    proposed_domain=str(row["proposed_domain"] or ""),
                    proposed_fact_type=str(row["proposed_fact_type"] or ""),
                    risk=item_risk,
                    salience=salience,
                    source_kind="",
                    created_at=str(row["created_at"] or ""),
                    wait_hours=round(wait, 1),
                    evidence_count=evidence_count,
                    uncertainty_error=uncertainty_error,
                    status=str(row["status"] or "review_required"),
                ))

        items.sort(key=lambda x: (RISK_ORDER.get(x.risk, 99), -x.wait_hours))
        return items

    def get_pending_item(self, candidate_id: str) -> dict | None:
        """取单条待审候选的**全文**与拍板上下文（issue #13）。

        ``list_pending`` 出于列表体积把 ``content`` 截到 200 字；人工要判
        一条事实该不该进库，看截断的正文等于闭眼签。此处返回完整 content
        及风险/等待/不确定性等拍板所需字段。

        只读：不改任何状态。终态（committed/rejected/...）不属待审，返回
        None——不得经此口子把已处理过的候选重新捞回"待办"。
        """
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return None
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """SELECT candidate_id, content, summary, status,
                          proposed_owner_principal, proposed_domain,
                          proposed_fact_type, uncertainty_json,
                          created_at, updated_at
                   FROM candidate_facts
                   WHERE candidate_id=? AND status IN
                         ('review_required', 'human_review')""",
                (candidate_id.strip(),),
            ).fetchone()
        if not row:
            return None

        uncertainty: dict = {}
        uncertainty_error = ""
        try:
            uncertainty = self._parse_uncertainty(row["uncertainty_json"])
        except UncertaintyParseError as exc:
            uncertainty_error = str(exc)

        wait_hours = 0.0
        try:
            created = self._parse_created_at(row["created_at"])
            wait_hours = round(max(0.0, (datetime.now(TZ) - created).total_seconds() / 3600), 1)
        except ValueError as exc:
            uncertainty_error = self._join_errors(uncertainty_error, str(exc))

        return {
            "candidate_id": row["candidate_id"],
            "status": str(row["status"] or ""),
            "content": str(row["content"] or ""),
            "summary": str(row["summary"] or ""),
            "proposed_owner": str(row["proposed_owner_principal"] or ""),
            "proposed_domain": str(row["proposed_domain"] or ""),
            "proposed_fact_type": str(row["proposed_fact_type"] or ""),
            "risk": self._infer_risk(
                row["proposed_fact_type"], row["proposed_domain"], uncertainty),
            "salience": self._infer_salience(uncertainty),
            "wait_hours": wait_hours,
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "uncertainty_json": row["uncertainty_json"],
            "uncertainty_error": uncertainty_error,
        }

    def backlog_aging(self, owner: str | None = None) -> dict:
        """待审积压的老化画像（issue #13）。

        为什么需要它：生产曾积压 12 条 human_review 达 13 天无人发现，
        而原 `review_reminder` 只吐一个 ``oldest_wait_hours`` 数字——
        "13 小时"与"13 天"在输出里长得几乎一样，读完也判断不了该不该动。

        这里给分桶 + stale 的 ID 清单，让告警能自己说"有 N 条已过期"，
        而不是等人去解读一个数。

        ``needs_attention`` 只在出现 stale 时为真：aging 不该天天响，
        否则告警本身变成噪声。
        """
        by_bucket = {b: 0 for b in AGE_BUCKETS}
        stale_ids: list[str] = []
        oldest_hours = 0.0

        # 复用 list_pending 的过滤与 owner 作用域，避免两处口径分叉。
        for item in self.list_pending(owner=owner, limit=500):
            bucket = age_bucket(item.wait_hours)
            by_bucket[bucket] += 1
            if item.wait_hours > oldest_hours:
                oldest_hours = item.wait_hours
            if bucket == "stale":
                stale_ids.append(item.candidate_id)

        return {
            "total": sum(by_bucket.values()),
            "by_bucket": by_bucket,
            "oldest_hours": round(oldest_hours, 1),
            "oldest_days": round(oldest_hours / 24.0, 2),
            "stale_candidate_ids": stale_ids,
            "needs_attention": bool(stale_ids),
            "thresholds_hours": {"aging": AGING_HOURS, "stale": STALE_HOURS},
        }

    def summarize(self, owner: str | None = None) -> ReviewQueueSummary:
        items = self.list_pending(owner=owner)
        by_risk: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        by_owner: dict[str, int] = {}
        oldest = 0.0
        parse_errors = []

        for item in items:
            by_risk[item.risk] = by_risk.get(item.risk, 0) + 1
            by_domain[item.proposed_domain] = by_domain.get(item.proposed_domain, 0) + 1
            by_owner[item.proposed_owner] = by_owner.get(item.proposed_owner, 0) + 1
            if item.wait_hours > oldest:
                oldest = item.wait_hours
            if item.uncertainty_error:
                parse_errors.append({
                    "candidate_id": item.candidate_id,
                    "error": item.uncertainty_error,
                })

        return ReviewQueueSummary(
            total=len(items),
            by_risk=by_risk,
            by_domain=by_domain,
            by_owner=by_owner,
            oldest_wait_hours=oldest,
            items=items[:20],
            parse_errors=parse_errors,
        )

    @staticmethod
    def _join_errors(current: str, new: str) -> str:
        return "; ".join(part for part in (current, new) if part)

    @staticmethod
    def _parse_created_at(raw: Any) -> datetime:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("created_at is missing or not text")
        try:
            created = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at is not valid ISO-8601") from exc
        if created.tzinfo is None:
            return created.replace(tzinfo=timezone.utc).astimezone(TZ)
        return created.astimezone(TZ)

    @staticmethod
    def _parse_uncertainty(raw: Any) -> dict:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            return {"uncertainty_reasons": raw}
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {"uncertainty_reasons": parsed}
                raise UncertaintyParseError(
                    f"uncertainty_json parsed as {type(parsed).__name__}, expected dict or list"
                )
            except json.JSONDecodeError as e:
                raise UncertaintyParseError(
                    f"uncertainty_json parse error: {e}"
                ) from e
        raise UncertaintyParseError(
            f"uncertainty_json has unexpected type: {type(raw).__name__}"
        )

    @staticmethod
    def _infer_risk(fact_type: str, domain: str, uncertainty: dict) -> str:
        if fact_type == "iron_rule":
            return "high"
        if domain == "personal":
            return "high" if fact_type in ("iron_rule", "user_pref") else "medium"
        if fact_type == "event":
            return "medium"
        reasons = uncertainty.get("uncertainty_reasons")
        if isinstance(reasons, (list, tuple)):
            reason_strs = [str(r) for r in reasons]
            if any("secret" in r.lower() or "sensitive" in r.lower() for r in reason_strs):
                return "critical"
            if any("correction" in r.lower() for r in reason_strs):
                return "medium"
        return "low"

    @staticmethod
    def _infer_salience(uncertainty: dict) -> float:
        raw = uncertainty.get("salience", 0.5)
        if raw is None or isinstance(raw, bool):
            return 0.5
        try:
            val = float(raw)
            if not math.isfinite(val):
                return 0.5
            return max(0.0, min(1.0, val))
        except (TypeError, ValueError):
            return 0.5