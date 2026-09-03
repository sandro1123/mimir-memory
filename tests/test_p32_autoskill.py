"""Mímir v14.0 — WikiSkill 技能自动编译流水线 (spec 阶段四任务1).

Traces (L0) ──▶ Mímir Wiki (L1/L2) ──▶ Hermes Skills (L3) 三层演化链：
- L0 执行痕迹（trace facts）记录任务成功执行。
- AutoSkillService.record_success 按主题沉淀成功 trace。
- 胜任经验（成功 ≥3 次且反馈良好：成员零 negative）由
  compile_wiki_candidates 自动提炼为 skill 候选。
- promote_to_skill 一键审批：物化为 L3 skill fact，检索面通过
  LAYER3_FACT_TYPES 自动全量挂载。

工程四严律：TDD 先行 / 不可变事件流 / 绝对路径安全 / 全量回归。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.autoskill import AutoSkillError, AutoSkillService
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact, FACT_TYPES, TombstoneFact
from mimir_v8.store import CanonicalStore, utc_now


def _trace(
    store,
    content,
    *,
    owner="mentor",
    fact_type="pattern",
    visibility="all",
    domain="knowledge",
    human_status="confirmed",
):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            visibility=visibility,
            sensitivity="internal",
            egress_policy="local_only",
            human_status=human_status,
        ),
        actor_principal=owner,
    )["fact_id"]


def _feedback(store, fact_id, feedback_type, *, submitted_by="mentor"):
    """Seed a learning_feedback row against a fact (canonical table)."""
    with store.transaction() as connection:
        fid = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO learning_feedback(
                   feedback_id, fact_id, feedback_type, feedback_text,
                   submitted_by, idempotency_key, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                fid,
                fact_id,
                feedback_type,
                f"test feedback {feedback_type}",
                submitted_by,
                str(uuid.uuid4()),
                utc_now(),
            ),
        )
    return fid


def _seed_topic(store, svc, topic, *, count=3, label=None):
    """Record `count` successful traces under one topic."""
    tids = []
    for i in range(count):
        tid = _trace(store, f"{label or topic} 步骤 {i}")
        svc.record_success(
            trace_id=tid, topic=topic, actor_principal="mentor"
        )
        tids.append(tid)
    return tids


class TestSkillFactTypeRegistered(unittest.TestCase):
    def test_skill_in_fact_types(self):
        self.assertIn("skill", FACT_TYPES)


class TestAutoSkillService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CanonicalStore(self.root / "canonical.db")
        self.svc = AutoSkillService(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    # ── record_success: L0 trace → topic aggregation ────────────────────

    def test_record_success_counts(self):
        tids = _seed_topic(self.store, self.svc, "k8s-crashloop", count=2)
        result = self.svc.record_success(
            trace_id=_trace(self.store, "排查 k8s CrashLoopBackOff 三"),
            topic="k8s-crashloop",
            actor_principal="mentor",
        )
        self.assertEqual(result["success_count"], 3)
        self.assertEqual(result["topic"], "k8s-crashloop")

    def test_record_success_rejects_unknown_trace(self):
        with self.assertRaises(AutoSkillError):
            self.svc.record_success(
                trace_id="nonexistent", topic="t", actor_principal="mentor"
            )

    def test_record_success_rejects_inert_trace(self):
        """A tombstoned trace is not competent evidence (Fail-Closed)."""
        tid = _trace(self.store, "排障痕迹：磁盘清理步骤")
        self.svc.record_success(
            trace_id=tid, topic="disk-cleanup", actor_principal="mentor"
        )
        fact = self.store.get_fact(tid)
        self.store.tombstone_fact(
            TombstoneFact(
                fact_id=tid, expected_version=fact["current_version"],
                reason="stale",
            ),
            actor_principal="mentor",
        )
        with self.assertRaises(AutoSkillError):
            self.svc.record_success(
                trace_id=tid, topic="disk-cleanup", actor_principal="mentor"
            )

    def test_record_success_is_idempotent_per_trace(self):
        """Re-recording the same trace does not double count."""
        tid = _trace(self.store, "唯一 trace")
        first = self.svc.record_success(
            trace_id=tid, topic="topic-x", actor_principal="mentor"
        )
        second = self.svc.record_success(
            trace_id=tid, topic="topic-x", actor_principal="mentor"
        )
        self.assertEqual(first["success_count"], 1)
        self.assertEqual(second["success_count"], 1)

    # ── compile: 胜任门槛 (success >= 3 AND zero negative feedback) ─────

    def test_compile_requires_three_successes(self):
        _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        _seed_topic(self.store, self.svc, "too-few", count=2)
        candidates = self.svc.compile_wiki_candidates()
        topics = [c["topic"] for c in candidates]
        self.assertIn("k8s-drain", topics)
        self.assertNotIn("too-few", topics)

    def test_compile_blocked_by_negative_feedback_on_any_member(self):
        """反馈良好 = 成员零 negative feedback。任一成员带
        incorrect/harmful 反馈即冻结该 topic 的晋升（Fail-Closed）。"""
        tids = _seed_topic(self.store, self.svc, "gpu-hot-migration", count=3)
        _feedback(self.store, tids[0], "harmful")
        candidates = self.svc.compile_wiki_candidates()
        self.assertEqual(candidates, [])

    def test_compile_is_idempotent(self):
        _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        first = self.svc.compile_wiki_candidates()
        second = self.svc.compile_wiki_candidates()
        self.assertEqual(
            [c["topic"] for c in first], [c["topic"] for c in second]
        )

    def test_compile_candidate_carries_evidence(self):
        tids = _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        candidates = self.svc.compile_wiki_candidates()
        cand = next(c for c in candidates if c["topic"] == "k8s-drain")
        self.assertEqual(cand["success_count"], 3)
        self.assertEqual(set(cand["trace_ids"]), set(tids))
        self.assertIn("content", cand)

    # ── promote_to_skill: 一键审批 → L3 skill fact ──────────────────────

    def test_promote_materializes_skill_fact(self):
        _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        result = self.svc.promote_to_skill("k8s-drain", actor_principal="mentor")
        fact = self.store.get_fact(result["skill_fact_id"])
        self.assertEqual(fact["fact_type"], "skill")
        self.assertEqual(fact["human_status"], "confirmed")
        self.assertEqual(fact["status"], "active")

    def test_promote_rejects_below_threshold(self):
        _seed_topic(self.store, self.svc, "too-few", count=2)
        with self.assertRaises(AutoSkillError):
            self.svc.promote_to_skill("too-few", actor_principal="mentor")

    def test_promote_rejects_negative_feedback(self):
        tids = _seed_topic(self.store, self.svc, "gpu-hot-migration", count=3)
        _feedback(self.store, tids[1], "incorrect")
        with self.assertRaises(AutoSkillError):
            self.svc.promote_to_skill(
                "gpu-hot-migration", actor_principal="mentor"
            )

    def test_promote_is_idempotent(self):
        """Same topic promoted twice yields the same skill fact."""
        _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        first = self.svc.promote_to_skill("k8s-drain", actor_principal="mentor")
        second = self.svc.promote_to_skill("k8s-drain", actor_principal="mentor")
        self.assertEqual(first["skill_fact_id"], second["skill_fact_id"])
        # and no duplicate fact was created
        with self.store.connect() as connection:
            n = connection.execute(
                "SELECT COUNT(*) c FROM facts WHERE fact_type='skill'"
            ).fetchone()["c"]
        self.assertEqual(n, 1)

    # ── retrieval: L3 挂载（LAYER3_FACT_TYPES 含 skill）────────────────

    def test_skill_rides_l3_layer_sweep(self):
        """skill 入 LAYER3_FACT_TYPES —— 检索面自动装配，全量挂载。"""
        from mimir_v8.query import QueryKernel, QueryRequest

        kernel = QueryKernel(self.store)
        self.assertIn("skill", kernel.LAYER3_FACT_TYPES)
        _seed_topic(self.store, self.svc, "k8s-drain", count=3)
        promoted = self.svc.promote_to_skill(
            "k8s-drain", actor_principal="mentor"
        )
        result = kernel.search(
            QueryRequest(
                text="k8s 节点排障怎么办", principal_id="mentor",
                roles=("mentor",),
            )
        )
        ids = [r["fact_id"] for r in result["results"]]
        self.assertIn(promoted["skill_fact_id"], ids)


class TestAutoSkillAPI(unittest.TestCase):
    """/v14/skills/* — REST 面：scope 门与胜任门槛不可绕过。"""

    def setUp(self):
        import hashlib
        import json

        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.query import QueryKernel

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canonical.db")
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": "mentor",
                        "token_sha256": hashlib.sha256(
                            b"tok-mentor"
                        ).hexdigest(),
                        "scopes": ["read", "write", "manage"],
                        "roles": [],
                        "admin": False,
                    },
                    {
                        "id": "ingestor",
                        "token_sha256": hashlib.sha256(
                            b"tok-ingest"
                        ).hexdigest(),
                        "scopes": ["ingest"],
                        "roles": [],
                        "admin": False,
                    },
                ]
            }),
            encoding="utf-8",
        )
        self.client = TestClient(
            create_app(
                ServiceContext(
                    store=self.store,
                    token_store=TokenStore(token_path),
                    query=QueryKernel(self.store),
                )
            ),
            raise_server_exceptions=False,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_success_endpoint(self):
        tid = _trace(self.store, "排查 k8s CrashLoopBackOff 步骤")
        r = self.client.post(
            "/v14/skills/record-success",
            json={"trace_id": tid, "topic": "k8s-crashloop"},
            headers={"Authorization": "Bearer tok-mentor"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["success_count"], 1)

    def test_candidates_endpoint_lists_competent_topics(self):
        _seed_topic(self.store, AutoSkillService(self.store),
                    "k8s-drain", count=3)
        r = self.client.get(
            "/v14/skills/candidates",
            headers={"Authorization": "Bearer tok-mentor"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        topics = [c["topic"] for c in r.json()["candidates"]]
        self.assertIn("k8s-drain", topics)

    def test_promote_endpoint_and_scope_gate(self):
        svc = AutoSkillService(self.store)
        _seed_topic(self.store, svc, "k8s-drain", count=3)
        # promote requires manage scope — ingest-only is 403
        r = self.client.post(
            "/v14/skills/promote",
            json={"topic": "k8s-drain"},
            headers={"Authorization": "Bearer tok-ingest"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post(
            "/v14/skills/promote",
            json={"topic": "k8s-drain"},
            headers={"Authorization": "Bearer tok-mentor"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.store.get_fact(r.json()["skill_fact_id"])["fact_type"],
            "skill",
        )
        # below-threshold topic is rejected with 409, not silently promoted
        r = self.client.post(
            "/v14/skills/promote",
            json={"topic": "too-few"},
            headers={"Authorization": "Bearer tok-mentor"},
        )
        self.assertEqual(r.status_code, 409, r.text)

    def test_promote_requires_authentication(self):
        r = self.client.post("/v14/skills/promote", json={"topic": "x"})
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
