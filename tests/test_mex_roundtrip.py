# -*- coding: utf-8 -*-
"""1.1.0 Task 3 — MEX v1 记忆交换格式（export/import + COGX 适配器）。

Roadmap 差距#2：跨系统导入导出（借 Cognee 四家适配器格局反向受益）。

信封：{"mex_version": 1, "source_node", "exported_at", "integrity":
{section: sha256}, "counts": {...}, "facts": [...], "fact_versions": [...],
"evidence": [...], "sources": [...]}

导入语义（宁缺勿脏，绝不当脏数据管道）：
- 同 fact_id + 同 content_hash → skip（幂等重放）
- 同 fact_id + 异 content_hash → skip + conflicts 点名（本地优先，绝不覆盖）
- 新 fact_id → 原样插 facts/fact_versions/evidence（保 fact_id/version 身份）
- integrity 校验失败 → 整包拒绝（ValidationError）
- dry_run → 只算报告不落库

TDD RED → GREEN.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.mex import (MEX_VERSION, ValidationError, cogx_to_mex,
                          mex_export, mex_import)
from mimir_v8.schema import CreateFact, UpdateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, *, owner="mentor", fact_type="event"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        domain="system", fact_type=fact_type, visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


def _bump(store, fid, content):
    store.update_fact(UpdateFact(
        fact_id=fid, content=content, expected_version=1,
        change_reason="mex 测试"), actor_principal="mentor")


class TestMexExport(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CanonicalStore(Path(self._tmp.name) / "src.db")
        self.fid = _seed(self.store, "铁律：生产库只允许经 API 写入")
        _bump(self.store, self.fid, "铁律：生产库只允许经 API 写入（v2）")

    def test_envelope_shape(self):
        payload = mex_export(self.store, node_id="n100-primary")
        self.assertEqual(payload["mex_version"], MEX_VERSION)
        self.assertEqual(payload["source_node"], "n100-primary")
        self.assertEqual(payload["counts"]["facts"], 1)
        self.assertEqual(payload["counts"]["fact_versions"], 2)
        self.assertIn("facts", payload)
        self.assertEqual(payload["facts"][0]["fact_id"], self.fid)
        self.assertEqual(payload["facts"][0]["content"], "铁律：生产库只允许经 API 写入（v2）")

    def test_evidence_closure_roundtrip(self):
        """带证据链的事实：candidates/legacy_sources 段随行迁移，导入后证据可查。

        这正是本轮根因修复的关联路径——证据不挂 fact_id 列，而是经
        candidate_facts.committed_fact_id 二跳；MEX 必须按真实依赖闭包导出，
        半包导入必 FK 炸。
        """
        from mimir_v8.store import new_id, utc_now
        now = utc_now()
        fid = _seed(self.store, "有证据支撑的铁律")
        source_pk, conv_pk = new_id(), new_id()
        cand_pk, evid_pk = new_id(), new_id()
        with self.store.connect() as c:
            c.execute("INSERT INTO sources (source_id, source_kind, "
                      "retrieved_at) VALUES(?,?,?)",
                      (source_pk, "conversation", now))
            c.execute(
                "INSERT INTO conversation_sources (source_id, connector_type,"
                " connector_id, source_hash, title, owner_principal, "
                "retention_class, memory_mode, source_category, ingested_at, "
                "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (conv_pk, "mcp", "mentor", "h1", "纪要", "mentor",
                 "standard", "observe", "conversation", now, "{}"))
            c.execute(
                "INSERT INTO candidate_facts (candidate_id, status, content,"
                " summary, proposed_owner_principal, proposed_domain, "
                "proposed_fact_type, proposed_visibility, proposed_sensitivity,"
                " proposed_egress_policy, source_id, uncertainty_json, "
                "proposed_by, committed_fact_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cand_pk, "committed", "候选", "候选", "mentor", "knowledge",
                 "iron_rule", "all", "internal", "local_only", source_pk,
                 "[]", "mentor", fid, now, now))
            c.execute(
                "INSERT INTO candidate_evidence (evidence_id, candidate_id,"
                " source_id, quote_text_redacted, evidence_hash, added_by, "
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (evid_pk, cand_pk, conv_pk, "支撑原文片段", "eh", "mentor", now))
        payload = mex_export(self.store, node_id="n")
        self.assertEqual(payload["counts"]["candidates"], 1)
        self.assertEqual(payload["counts"]["evidence"], 1)
        self.assertEqual(payload["counts"]["legacy_sources"], 1)
        # 导入到空库后，证据必须经修复后的关联路径可查
        dst = CanonicalStore(Path(self._tmp.name) / "dst2.db")
        report = mex_import(dst, payload, actor_principal="mentor")
        self.assertEqual(report["imported"], 2)  # 两条事实（另一测试种子在 src 不在此包）
        from mimir_v8.query import QueryKernel
        ev = QueryKernel(dst, vector=None, fts=None,
                         graph=None)._evidence_for(fid, top_n=2)
        self.assertEqual(len(ev), 1)
        self.assertIn("支撑原文片段", ev[0]["quote_text"])

    def test_integrity_section_hashes(self):
        payload = mex_export(self.store, node_id="n100-primary")
        # 每个数据段都有 sha256；篡改段内容后校验必炸
        payload["facts"][0]["content"] = "导出后篡改"
        with self.assertRaises(ValidationError):
            mex_import(CanonicalStore(Path(self._tmp.name) / "t.db"),
                       payload, actor_principal="mentor")

    def test_since_filter(self):
        payload = mex_export(self.store, node_id="n",
                             since="2999-01-01T00:00:00+00:00")
        self.assertEqual(payload["counts"]["facts"], 0)

    def test_external_mode_respects_egress(self):
        """外发模式：只导 external_allowed；local_only 一律拒发。"""
        from mimir_v8.schema import CreateFact
        self.store.create_fact(CreateFact(
            content="可外发的公告", summary="公告", owner_principal="mentor",
            domain="knowledge", fact_type="reference", visibility="all",
            sensitivity="internal", egress_policy="external_allowed",
            human_status="confirmed"), actor_principal="mentor")
        payload = mex_export(self.store, node_id="n", external=True)
        self.assertEqual(payload["counts"]["facts"], 1)
        self.assertEqual(payload["facts"][0]["content"], "可外发的公告")


class TestMexImport(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = CanonicalStore(Path(self._tmp.name) / "src.db")
        self.dst_path = Path(self._tmp.name) / "dst.db"
        self.dst = CanonicalStore(self.dst_path)
        self.fid = _seed(self.src, "跨节点迁移的事实")
        _bump(self.src, self.fid, "跨节点迁移的事实 v2")
        self.payload = mex_export(self.src, node_id="src-node")

    def _fact_row(self, store, fid):
        with store.connect() as c:
            return c.execute("SELECT * FROM facts WHERE fact_id=?",
                             (fid,)).fetchone()

    def test_roundtrip_field_equal(self):
        report = mex_import(self.dst, self.payload, actor_principal="mentor")
        self.assertEqual(report["imported"], 1)
        self.assertEqual(report["skipped"], 0)
        src_row, dst_row = self._fact_row(self.src, self.fid), self._fact_row(self.dst, self.fid)
        for key in src_row.keys():
            self.assertEqual(src_row[key], dst_row[key], f"字段漂移: {key}")
        with self.dst.connect() as c:
            vers = c.execute("SELECT version, snapshot_json, "
                             "previous_version_hash FROM fact_versions "
                             "WHERE fact_id=? ORDER BY version",
                             (self.fid,)).fetchall()
        with self.src.connect() as c:
            src_vers = c.execute("SELECT version, snapshot_json, "
                                 "previous_version_hash FROM fact_versions "
                                 "WHERE fact_id=? ORDER BY version",
                                 (self.fid,)).fetchall()
        self.assertEqual([dict(v) for v in vers], [dict(v) for v in src_vers])
        self.assertEqual(vers[1]["previous_version_hash"],
                         src_vers[1]["previous_version_hash"])
        self.assertNotIn(vers[1]["previous_version_hash"], (None, ""))
        # 谱系链随行迁移且目标库可验证
        from mimir_v8.lineage import verify_lineage_chain
        self.assertEqual(verify_lineage_chain(self.dst, self.fid)["chain_status"],
                         "sealed_ok")

    def test_reimport_is_idempotent_skip(self):
        mex_import(self.dst, self.payload, actor_principal="mentor")
        second = mex_import(self.dst, self.payload, actor_principal="mentor")
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["conflicts"], [])

    def test_conflicting_content_never_overwrites(self):
        """宁缺勿脏：同 fact_id 异 content_hash → 本地保留，冲突点名。"""
        mex_import(self.dst, self.payload, actor_principal="mentor")
        # 本地改出不同内容（导入后 dst 已是 v2，expected 须跟上真实版本）
        with self.dst.connect() as c:
            cur = c.execute("SELECT current_version FROM facts WHERE fact_id=?",
                            (self.fid,)).fetchone()["current_version"]
        self.dst.update_fact(UpdateFact(
            fact_id=self.fid, content="本地修订版，不接受外来覆盖",
            expected_version=cur, change_reason="本地修订"),
            actor_principal="mentor")
        foreign = mex_export(self.src, node_id="src-node")
        report = mex_import(self.dst, foreign, actor_principal="mentor")
        self.assertEqual(report["imported"], 0)
        self.assertEqual([c["fact_id"] for c in report["conflicts"]],
                         [self.fid])
        self.assertIn("本地修订版", self._fact_row(self.dst, self.fid)["content"])

    def test_dry_run_writes_nothing(self):
        report = mex_import(self.dst, self.payload,
                            actor_principal="mentor", dry_run=True)
        self.assertEqual(report["would_import"], 1)
        self.assertIsNone(self._fact_row(self.dst, self.fid))

    def test_bad_mex_version_rejected(self):
        bad = dict(self.payload, mex_version=999)
        with self.assertRaises(ValidationError):
            mex_import(self.dst, bad, actor_principal="mentor")


class TestCogxAdapter(unittest.TestCase):
    """COGX→Mímir 导入器：最小样本内联（不外引文件）。"""

    COGX_SAMPLE = {
        "format": "cogx/0.1",
        "nodes": [
            {"id": "c1", "type": "MemoryFact",
             "properties": {"text": "用户偏好深色主题",
                            "category": "user_preference",
                            "created_at": "2026-09-01T10:00:00+00:00"}},
            {"id": "c2", "type": "MemoryFact",
             "properties": {"text": "项目使用 SQLite WAL",
                            "category": "project_config",
                            "created_at": "2026-09-02T11:00:00+00:00"}},
        ],
    }

    def test_cogx_maps_to_mex_envelope(self):
        payload = cogx_to_mex(self.COGX_SAMPLE, source_node="cogx-import")
        self.assertEqual(payload["mex_version"], MEX_VERSION)
        self.assertEqual(payload["counts"]["facts"], 2)
        by_content = {f["content"]: f for f in payload["facts"]}
        self.assertIn("用户偏好深色主题", by_content)
        mapped = by_content["用户偏好深色主题"]
        self.assertEqual(mapped["fact_type"], "user_pref")   # 类型映射
        self.assertEqual(mapped["domain"], "personal")       # 域映射
        # 原 id 保留在 legacy_id，供审计回溯
        self.assertEqual(mapped["legacy_id"], "c1")

    def test_cogx_into_store(self):
        payload = cogx_to_mex(self.COGX_SAMPLE, source_node="cogx-import")
        store = CanonicalStore(Path(self._tmp_dir) / "cogx.db")
        report = mex_import(store, payload, actor_principal="mentor")
        self.assertEqual(report["imported"], 2)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


if __name__ == "__main__":
    unittest.main()
