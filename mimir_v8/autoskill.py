"""Mímir v14.0 — WikiSkill 技能自动编译流水线 (spec 阶段四任务1).

确立 Traces (L0) ──▶ Mímir Wiki (L1/L2) ──▶ Hermes Skills (L3) 三层演化链：

- L0：执行痕迹（trace facts，可溯源到 ingestion 的对话/事件证据）。
- record_success 按主题沉淀成功 trace —— 每条 trace 至多记一次
  （幂等），不可变事件流 memory_events 记录每次记账。
- compile_wiki_candidates：胜任门槛 = 同主题成功 ≥3 次 且 成员
  零 negative feedback（incorrect/harmful/withdraw 任一即冻结，
  Fail-Closed）。满足门槛的主题自动提炼为 skill 候选。
- promote_to_skill 一键审批：人工审批物化 fact_type='skill' 的
  L3 fact（human_status='confirmed'），同主题重复审批幂等返回
  既有 skill fact。检索面通过 LAYER3_FACT_TYPES 挂载 skill 后
  自动全量装配。

设计原则（对齐 crystallize.py 先例）：候选行永不原地改写已决状态；
每一步生命周期事件都写入 memory_events；审批只追加新 fact，不回写
历史。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now

#: 胜任门槛：同主题成功执行次数下限（spec: 成功解决 ≥3 次）。
MIN_SKILL_SUCCESSES = 3
#: 反馈良好的定义：成员零 negative feedback。
NEGATIVE_FEEDBACK_TYPES = ("incorrect", "harmful", "withdraw")

V18_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS skill_topics (
        topic TEXT PRIMARY KEY,
        success_count INTEGER NOT NULL CHECK (success_count >= 0),
        trace_ids TEXT NOT NULL,
        last_success_at TEXT NOT NULL,
        skill_fact_id TEXT
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_skill_topics_pending
       ON skill_topics(success_count) WHERE skill_fact_id IS NULL""",
)


class AutoSkillError(RuntimeError):
    pass


class AutoSkillService:
    """Traces→Wiki→Skills 演化链的账本与门槛守卫。"""

    def __init__(self, store: CanonicalStore):
        self.store = store
        self._ensure_tables()

    # ── L0 → topic ledger ──────────────────────────────────────────────

    def record_success(
        self, trace_id: str, topic: str, actor_principal: str
    ) -> dict:
        """Record one successful execution trace under a topic.

        Idempotent per trace: re-recording the same trace never inflates
        the count. Rejects unknown/tombstoned traces (Fail-Closed) and
        blank topics. Appends an immutable memory_event per new recording.
        """
        if not topic or not topic.strip():
            raise AutoSkillError("topic is required")
        topic = topic.strip()
        now = utc_now()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            trace = connection.execute(
                "SELECT fact_id, status FROM facts WHERE fact_id=?",
                (trace_id,),
            ).fetchone()
            if trace is None:
                raise AutoSkillError(f"unknown trace: {trace_id}")
            if trace["status"] != "active":
                raise AutoSkillError(
                    f"trace {trace_id} is {trace['status']}"
                    " — not competent evidence"
                )
            row = connection.execute(
                "SELECT trace_ids FROM skill_topics WHERE topic=?",
                (topic,),
            ).fetchone()
            trace_ids = set(json.loads(row["trace_ids"])) if row else set()
            if trace_id in trace_ids:
                # idempotent replay: no double count, no second event
                return {
                    "topic": topic,
                    "trace_id": trace_id,
                    "success_count": len(trace_ids),
                    "replayed": True,
                }
            trace_ids.add(trace_id)
            if row:
                connection.execute(
                    """UPDATE skill_topics
                    SET success_count=?, trace_ids=?, last_success_at=?
                    WHERE topic=?""",
                    (len(trace_ids), json.dumps(sorted(trace_ids)),
                     now, topic),
                )
            else:
                connection.execute(
                    """INSERT INTO skill_topics(
                        topic, success_count, trace_ids, last_success_at
                    ) VALUES(?,?,?,?)""",
                    (topic, len(trace_ids),
                     json.dumps(sorted(trace_ids)), now),
                )
            self._event(
                connection, topic, "autoskill.recorded",
                actor=actor_principal, now=now,
                payload={"topic": topic, "trace_id": trace_id,
                         "success_count": len(trace_ids)},
            )
        return {
            "topic": topic,
            "trace_id": trace_id,
            "success_count": len(trace_ids),
            "replayed": False,
        }

    # ── compile: 胜任门槛（成功 ≥3 且零 negative）──────────────────────

    def compile_wiki_candidates(self) -> list[dict]:
        """Surface topics that cleared the competence threshold.

        A topic qualifies when its success count reached
        MIN_SKILL_SUCCESSES and none of its member traces carries a
        negative feedback row (Fail-Closed on evidence quality).
        Topics already promoted (skill_fact_id set) are not re-listed.
        """
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                """SELECT topic, success_count, trace_ids, skill_fact_id
                FROM skill_topics
                WHERE success_count >= ? AND skill_fact_id IS NULL
                ORDER BY success_count DESC, topic""",
                (MIN_SKILL_SUCCESSES,),
            ).fetchall()
            candidates = []
            for row in rows:
                trace_ids = json.loads(row["trace_ids"])
                if self._has_negative_feedback(connection, trace_ids):
                    continue
                contents = self._trace_contents(connection, trace_ids)
                candidates.append({
                    "topic": row["topic"],
                    "success_count": row["success_count"],
                    "trace_ids": trace_ids,
                    "content": self._compile_content(
                        row["topic"], contents
                    ),
                })
            return candidates

    # ── promote_to_skill: 一键审批 → L3 skill fact ─────────────────────

    def promote_to_skill(self, topic: str, actor_principal: str) -> dict:
        """Human approval: materialize a competent topic as an L3 skill.

        Enforces the same competence threshold at promotion time (the
        ledger may have changed between compile and approve). Idempotent:
        re-approving an already-skilled topic returns the existing skill
        fact without creating a duplicate.
        """
        topic = (topic or "").strip()
        if not topic:
            raise AutoSkillError("topic is required")
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                "SELECT * FROM skill_topics WHERE topic=?", (topic,)
            ).fetchone()
            if row is None:
                raise AutoSkillError(f"unknown topic: {topic}")
            if row["skill_fact_id"]:
                return {
                    "topic": topic,
                    "skill_fact_id": row["skill_fact_id"],
                    "replayed": True,
                }
            trace_ids = json.loads(row["trace_ids"])
            if len(trace_ids) < MIN_SKILL_SUCCESSES:
                raise AutoSkillError(
                    f"topic {topic} has {len(trace_ids)} successes"
                    f" — needs >= {MIN_SKILL_SUCCESSES} (competence threshold)"
                )
            if self._has_negative_feedback(connection, trace_ids):
                raise AutoSkillError(
                    f"topic {topic} has negative feedback on its traces"
                    " — promotion frozen (Fail-Closed)"
                )
            contents = self._trace_contents(connection, trace_ids)
            now = utc_now()
            skill_fact_id = self._materialize(
                connection, topic, contents, len(trace_ids), actor_principal
            )
            connection.execute(
                "UPDATE skill_topics SET skill_fact_id=? WHERE topic=?",
                (skill_fact_id, topic),
            )
            self._event(
                connection, topic, "autoskill.promoted",
                actor=actor_principal, now=now,
                payload={"topic": topic, "skill_fact_id": skill_fact_id,
                         "success_count": len(trace_ids)},
            )
        return {
            "topic": topic,
            "skill_fact_id": skill_fact_id,
            "replayed": False,
        }

    # ── internals ──────────────────────────────────────────────────────

    def _has_negative_feedback(
        self, connection: sqlite3.Connection, trace_ids: list[str]
    ) -> bool:
        if not trace_ids:
            return False
        placeholders = ",".join("?" * len(trace_ids))
        row = connection.execute(
            f"""SELECT COUNT(*) AS c FROM learning_feedback
            WHERE fact_id IN ({placeholders})
              AND feedback_type IN (?,?,?)""",
            (*trace_ids, *NEGATIVE_FEEDBACK_TYPES),
        ).fetchone()
        return row["c"] > 0

    def _trace_contents(
        self, connection: sqlite3.Connection, trace_ids: list[str]
    ) -> list[str]:
        if not trace_ids:
            return []
        placeholders = ",".join("?" * len(trace_ids))
        rows = connection.execute(
            f"""SELECT content FROM facts
            WHERE fact_id IN ({placeholders}) AND status='active'
            ORDER BY recorded_at""",
            trace_ids,
        ).fetchall()
        return [row["content"] for row in rows]

    @staticmethod
    def _compile_content(topic: str, contents: list[str]) -> str:
        """Deterministic skill content: topic headline + step list."""
        lines = [f"[skill] {topic}"]
        for content in contents:
            text = (content or "").strip().splitlines()
            if text:
                lines.append(f"- {text[0]}")
        return "\n".join(lines)

    def _materialize(
        self,
        connection: sqlite3.Connection,
        topic: str,
        contents: list[str],
        success_count: int,
        actor_principal: str,
    ) -> str:
        """Create the L3 skill fact backing an approved topic."""
        from .schema import CreateFact

        content = self._compile_content(topic, contents)
        summary = (
            f"Skill compiled from {success_count} successful executions"
            f" on topic '{topic}'"
        )
        result = self.store.create_fact(
            CreateFact(
                content=content,
                owner_principal=actor_principal,
                domain="knowledge",
                fact_type="skill",
                summary=summary,
                confidence_score=0.9,
                human_status="confirmed",
            ),
            actor_principal,
            connection=connection,
        )
        return result["fact_id"]

    def _ensure_tables(self, connection: sqlite3.Connection | None = None) -> None:
        """Create skill_topics on first use (additive, idempotent)."""
        if connection is None:
            with self.store.connect() as owned:
                self._ensure_tables(owned)
            return
        for statement in V18_ADDITIVE_STATEMENTS:
            connection.execute(statement)

    def _event(
        self,
        connection: sqlite3.Connection,
        aggregate_id: str,
        event_type: str,
        *,
        actor: str,
        now: str,
        payload: dict,
    ) -> None:
        connection.execute(
            """INSERT INTO memory_events(
                event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), "autoskill", aggregate_id, 1, event_type,
             actor, new_id(), new_id(), now,
             json.dumps(payload), sha256_text(f"{aggregate_id}:{event_type}:{now}")),
        )
