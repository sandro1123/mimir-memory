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
                       c.created_at
                FROM candidate_facts c
                WHERE c.status = 'review_required'
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
                ))

        items.sort(key=lambda x: (RISK_ORDER.get(x.risk, 99), -x.wait_hours))
        return items

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