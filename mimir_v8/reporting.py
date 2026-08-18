"""Mímir v8.2 daily report and deep reading module.

Generates structured daily memory reports for Feishu/Obsidian delivery,
and performs LLM-powered deep reading of collected content.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .schema import MIMIR_VERSION, SCHEMA_VERSION
from .store import CanonicalStore
from .evaluator import Evaluator, EvaluationResult, PolicyDecision


TZ = timezone(timedelta(hours=8))
REPORT_VERSION = "v8.2-report-1"


@dataclass
class DailyReport:
    date: str
    version: str = REPORT_VERSION
    mimir_version: str = MIMIR_VERSION

    ingestion: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)
    reviews: dict = field(default_factory=dict)
    deep_reading: dict = field(default_factory=dict)
    governance: dict = field(default_factory=dict)
    system: dict = field(default_factory=dict)


class ReportGenerator:
    """Generates daily memory reports from Mimir store data."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def generate(self) -> DailyReport:
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        since = now - timedelta(hours=24)

        report = DailyReport(date=today)

        with self.store.connect() as connection:
            report.ingestion = self._ingestion_stats(connection, since)
            report.candidates = self._candidate_stats(connection, since)
            report.reviews = self._review_stats(connection, since)
            report.governance = self._governance_stats(connection)
            report.system = self._system_stats(connection)

        return report

    def _ingestion_stats(self, connection, since: datetime) -> dict:
        count = connection.execute(
            "SELECT COUNT(*) AS cnt FROM ingestion_runs WHERE started_at>=?", (since.isoformat(),)
        ).fetchone()["cnt"]
        msg_count = connection.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_messages WHERE created_at>=?", (since.isoformat(),)
        ).fetchone()["cnt"]
        redacted = connection.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_messages WHERE redaction_applied=1 AND created_at>=?",
            (since.isoformat(),),
        ).fetchone()["cnt"]
        return {
            "ingestion_runs": count,
            "messages": msg_count,
            "redacted": redacted,
        }

    def _candidate_stats(self, connection, since: datetime) -> dict:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS cnt FROM candidate_facts GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["cnt"] for r in rows}

        new_rows = connection.execute(
            "SELECT status, COUNT(*) AS cnt FROM candidate_facts WHERE created_at>=? GROUP BY status",
            (since.isoformat(),),
        ).fetchall()
        new_by_status = {r["status"]: r["cnt"] for r in new_rows}

        review_required = connection.execute(
            "SELECT COUNT(*) AS cnt FROM candidate_facts WHERE status='review_required'"
        ).fetchone()["cnt"]

        oldest = connection.execute(
            "SELECT created_at FROM candidate_facts WHERE status='review_required' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        oldest_wait = 0
        if oldest and oldest["created_at"]:
            created = datetime.fromisoformat(oldest["created_at"])
            oldest_wait = round((datetime.now(TZ) - created.replace(tzinfo=TZ) if created.tzinfo else datetime.now(TZ) - created).total_seconds() / 3600, 1)

        return {
            "total_by_status": by_status,
            "new_today": new_by_status,
            "review_required": review_required,
            "oldest_wait_hours": oldest_wait,
        }

    def _review_stats(self, connection, since: datetime) -> dict:
        approved = connection.execute(
            "SELECT COUNT(*) AS cnt FROM review_actions WHERE action='approve' AND created_at>=?",
            (since.isoformat(),),
        ).fetchone()["cnt"]
        rejected = connection.execute(
            "SELECT COUNT(*) AS cnt FROM review_actions WHERE action='reject' AND created_at>=?",
            (since.isoformat(),),
        ).fetchone()["cnt"]
        committed = connection.execute(
            "SELECT COUNT(*) AS cnt FROM candidate_facts WHERE status='committed' AND updated_at>=?",
            (since.isoformat(),),
        ).fetchone()["cnt"]
        return {
            "approved": approved,
            "rejected": rejected,
            "committed": committed,
        }

    def _governance_stats(self, connection) -> dict:
        total_facts = connection.execute("SELECT COUNT(*) AS cnt FROM facts WHERE status='active'").fetchone()["cnt"]
        tombstoned = connection.execute("SELECT COUNT(*) AS cnt FROM facts WHERE status='tombstoned'").fetchone()["cnt"]
        return {
            "active_facts": total_facts,
            "tombstoned_facts": tombstoned,
        }

    def _system_stats(self, connection) -> dict:
        return {
            "mimir_version": MIMIR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "event_head": self._get_event_head(connection),
            "projector_status": "ok",
        }

    @staticmethod
    def _get_event_head(connection) -> int:
        row = connection.execute("SELECT MAX(event_seq) AS seq FROM memory_events").fetchone()
        return row["seq"] if row and row["seq"] else 0

    def to_feishu_card(self, report: DailyReport) -> dict:
        lines = []
        ing = report.ingestion
        cand = report.candidates
        rev = report.reviews
        gov = report.governance
        sys = report.system

        lines.append(f"📋 **Mímir 记忆日报 — {report.date}**")
        lines.append("")

        lines.append("**📥 今日摄入**")
        lines.append(f"· 对话摄入：{ing.get('ingestion_runs', 0)} 次，{ing.get('messages', 0)} 条消息")
        if ing.get("redacted", 0):
            lines.append(f"· 脱敏处理：{ing['redacted']} 条含敏感信息")
        lines.append("")

        lines.append("**📊 Candidate 审核队列**")
        lines.append(f"· 待审核：{cand.get('review_required', 0)} 条")
        lines.append(f"· 最长等待：{cand.get('oldest_wait_hours', 0)} 小时")
        lines.append("")

        lines.append("**✅ 今日审核**")
        lines.append(f"· 批准：{rev.get('approved', 0)} 条")
        lines.append(f"· 拒绝：{rev.get('rejected', 0)} 条")
        lines.append(f"· 已提交：{rev.get('committed', 0)} 条")
        lines.append("")

        lines.append("**🧹 系统状态**")
        lines.append(f"· 活跃事实：{gov.get('active_facts', 0)} 条")
        lines.append(f"· 版本：{sys.get('mimir_version', '?')} / schema {sys.get('schema_version', '?')}")
        lines.append(f"· 事件序列：{sys.get('event_head', 0)}")

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📋 Mímir 记忆日报 {report.date}"},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": "\n".join(lines)},
                ],
            },
        }

    def to_obsidian(self, report: DailyReport) -> str:
        lines = [
            "---",
            f"created: {report.date}",
            "type: mimir-daily-report",
            f"version: {REPORT_VERSION}",
            "---",
            "",
            f"# Mímir 记忆日报 — {report.date}",
            "",
            "## 📥 今日摄入",
            f"- 对话摄入：{report.ingestion.get('ingestion_runs', 0)} 次，{report.ingestion.get('messages', 0)} 条消息",
            "",
            "## 📊 Candidate 审核队列",
            f"- 待审核：{report.candidates.get('review_required', 0)} 条",
            f"- 最长等待：{report.candidates.get('oldest_wait_hours', 0)} 小时",
            "",
            "## ✅ 今日审核",
            f"- 批准：{report.reviews.get('approved', 0)} 条",
            f"- 拒绝：{report.reviews.get('rejected', 0)} 条",
            f"- 已提交：{report.reviews.get('committed', 0)} 条",
            "",
            "## 🧹 系统状态",
            f"- 活跃事实：{report.governance.get('active_facts', 0)} 条",
            f"- 版本：{report.system.get('mimir_version', '?')} / schema {report.system.get('schema_version', '?')}",
            f"- 事件序列：{report.system.get('event_head', 0)}",
            "",
        ]
        return "\n".join(lines)


@dataclass
class ReadingResult:
    action: str  # kept / discarded / error
    title: str = ""
    source: str = ""
    salience: float = 0.0
    summary: str = ""
    insights: list[str] = field(default_factory=list)
    candidates_created: int = 0
    log_path: str = ""
    error: str = ""


class DeepReader:
    """LLM-powered deep reading of collected content.

    Evaluates content value, extracts insights, writes to Obsidian,
    and creates Candidates for valuable insights.
    """

    def __init__(self, store: CanonicalStore, evaluator: Evaluator | None = None):
        self.store = store
        self.evaluator = evaluator or Evaluator()
        self.vault_path = Path.home() / "obsidian-vault" / "Sandro's Vault"
        self.learn_dir = self.vault_path / "10-项目" / "Mímir 联邦记忆系统" / "学习日志"

    def read(self, content: str, source: str, title: str = "") -> ReadingResult:
        if not content or not content.strip():
            return ReadingResult(action="error", source=source, error="empty content")

        eval_result = self.evaluator.evaluate(content)
        decision = self.evaluator.decide(eval_result)

        if decision.action == "reject":
            return ReadingResult(
                action="discarded", title=title, source=source,
                salience=0.0, summary="rejected: sensitive content",
            )

        if decision.action == "discard":
            return ReadingResult(
                action="discarded", title=title, source=source,
                salience=eval_result.salience,
                summary=decision.reason,
            )

        insights = self._extract_insights(content, eval_result)
        log_path = self._write_reading_log(content, eval_result, insights, title, source)

        return ReadingResult(
            action="kept",
            title=title,
            source=source,
            salience=eval_result.salience,
            summary=eval_result.summary,
            insights=insights,
            candidates_created=0,
            log_path=log_path,
        )

    def _extract_insights(self, content: str, eval_result: EvaluationResult) -> list[str]:
        insights = []
        if eval_result.summary:
            insights.append(eval_result.summary)
        sentences = re.split(r"[。！？\n]", content)
        for s in sentences:
            s = s.strip()
            if len(s) > 20 and any(kw in s for kw in ("必须", "应该", "偏好", "记住", "不要", "禁止", "配置", "规则")):
                insights.append(s[:200])
        return insights[:5]

    def _write_reading_log(
        self, content: str, eval_result: EvaluationResult,
        insights: list[str], title: str, source: str,
    ) -> str:
        now = datetime.now(TZ)
        slug = now.strftime("%Y-%m-%d-%H%M%S")
        safe_title = re.sub(r"[^\w\-]", "_", title[:30]) if title else "deep_reading"
        filename = f"{slug}-{safe_title}.md"
        filepath = self.learn_dir / filename

        lines = [
            "---",
            f"created: {now.strftime('%Y-%m-%d')}",
            "type: deep-reading-auto",
            f"source: {source}",
            f"title: {title or '深度研读'}",
            f"salience: {eval_result.salience}",
            f"domain: {eval_result.domain}",
            f"fact_type: {eval_result.fact_type}",
            "status: completed",
            "---",
            "",
            f"# 深度研读：{title or '未命名'}",
            "",
            f"## 来源",
            f"- {source}",
            "",
            f"## 价值评估",
            f"- 价值分：{eval_result.salience}",
            f"- 风险等级：{eval_result.risk}",
            f"- 领域：{eval_result.domain}",
            f"- 类型：{eval_result.fact_type}",
            "",
            f"## 摘要",
            f"{eval_result.summary}",
            "",
            "## 核心观点",
        ]
        for i, insight in enumerate(insights, 1):
            lines.append(f"{i}. {insight}")
        lines.append("")
        lines.append("## LLM 推理")
        lines.append(f"{eval_result.reasoning}")
        lines.append("")
        lines.append("## 相关事实 (double links)")
        for target in self._related_fact_links(content):
            lines.append(f"- [[{target}]]")
        lines.append("")
        lines.append("## Backlinks")
        lines.append("<!-- 反向链接由 Obsidian 从全文 [[相关事实]] 自动构建 -->")
        lines.append("")
        lines.append("## 原文")
        lines.append(f"{content[:2000]}")

        self.learn_dir.mkdir(parents=True, exist_ok=True)
        filepath.write_text("\n".join(lines), encoding="utf-8")
        return str(filepath)

    def _related_fact_links(self, content: str, limit: int = 8) -> list[str]:
        """Find active facts sharing topic terms and return their Wikilinks."""
        from .wikilink import note_slug
        try:
            keywords = set(re.findall(r"[a-zA-Z0-9_]{4,}", content.lower()))
        except (TypeError, ValueError):
            return []
        stop = {"mimir", "用户", "system", "this", "that", "with", "from"}
        keywords -= stop
        if not keywords:
            return []
        placeholders = ",".join("?" for _ in keywords)
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                f"""SELECT fact_id, content_hash, summary, content
                FROM facts WHERE status='active' LIMIT 200"""
            ).fetchall()
        matches: list[str] = []
        for row in rows:
            blob = f"{row['summary']} {row['content']}".lower()
            for word in keywords:
                if f" {word} " in f" {blob} ":
                    matches.append(note_slug(
                        row["fact_id"], row["content_hash"], row["summary"]))
                    break
        return sorted(set(matches))[:limit]