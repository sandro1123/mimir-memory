# -*- coding: utf-8 -*-
"""1.1.0 根因修复回归 — 证据召回腿在真实 schema 上必须能取到引文。

1.0-C2 的 _evidence_for 查了不存在的列（e.fact_id / e.quote_text），
OperationalError 被裸 except 吞成 []，于是所有检索的 evidence 恒空、且
test_c23 只断言「键在、是列表」对空也判绿（夹具比生产宽的镜像：它把病
态行为断言成了契约）。本文件**用真实 fresh schema 三表链**造一条已提交
候选事实 + 证据，断言 _evidence_for 与端到端 search 都取到非空引文——
修复前的 SQL 会让这两处恒空或抛错，正是证伪点。

TDD：对已修的 query.py 应绿；把 query.py 回退到 C2 原样应红。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore, new_id, utc_now


def _seed_fact(store, content, *, owner="mentor", fact_type="iron_rule"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        domain="knowledge", fact_type=fact_type, visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


def _wire_evidence(store, fact_id, *, quote="生产库只允许经 API 写入的原文片段",
                   owner="mentor"):
    """在真实 schema 上串起 sources→candidate→evidence→committed_fact 链。

    走真表真列，不自造宽表——宽表夹具正是 C2 装绿的帮凶。
    """
    now = utc_now()
    source_pk = new_id()
    conv_pk = new_id()
    candidate_pk = new_id()
    evidence_pk = new_id()
    with store.connect() as c:
        # candidate_facts.source_id → sources（旧表）
        c.execute(
            "INSERT INTO sources (source_id, source_kind, retrieved_at) "
            "VALUES(?,?,?)", (source_pk, "conversation", now))
        # candidate_evidence.source_id → conversation_sources（新表）
        c.execute(
            "INSERT INTO conversation_sources (source_id, connector_type, "
            "connector_id, source_hash, title, owner_principal, "
            "retention_class, memory_mode, source_category, ingested_at, "
            "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (conv_pk, "mcp", "mentor", "h1", "会话纪要", owner,
             "standard", "observe", "conversation", now, "{}"))
        # candidate 已提交到该 fact（committed_fact_id 是关键二跳）
        c.execute(
            "INSERT INTO candidate_facts (candidate_id, status, content, "
            "summary, proposed_owner_principal, proposed_domain, "
            "proposed_fact_type, proposed_visibility, proposed_sensitivity, "
            "proposed_egress_policy, source_id, uncertainty_json, "
            "proposed_by, committed_fact_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_pk, "committed", "候选原文", "候选", owner,
             "knowledge", "iron_rule", "all", "internal", "local_only",
             source_pk, "[]", owner, fact_id, now, now))
        c.execute(
            "INSERT INTO candidate_evidence (evidence_id, candidate_id, "
            "source_id, quote_text_redacted, evidence_hash, added_by, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (evidence_pk, candidate_pk, conv_pk, quote, "eh1", owner, now))


def _kernel(store):
    return QueryKernel(store, vector=None, fts=None, graph=None)


class TestEvidenceRecallLive(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CanonicalStore(Path(self._tmp.name) / "ev.db")
        self.fid = _seed_fact(self.store, "生产库只允许经 API 写入，禁止直连")
        _wire_evidence(self.store, self.fid)

    def test_evidence_for_returns_real_quote(self):
        """直调 _evidence_for 必须取到引文（修复前恒 []，因列名/JOIN 错）。"""
        k = _kernel(self.store)
        ev = k._evidence_for(self.fid, top_n=2)
        self.assertEqual(len(ev), 1)
        self.assertIn("生产库只允许经 API 写入", ev[0]["quote_text"])
        self.assertEqual(ev[0]["source_name"], "会话纪要")
        self.assertTrue(ev[0]["message_ts"])

    def test_search_result_carries_nonempty_evidence(self):
        """端到端：检索命中结果的 evidence 非空（修复前这里恒空却判绿）。"""
        k = _kernel(self.store)
        r = k.search(QueryRequest(text="生产库 写入 直连",
                                 principal_id="mentor", limit=5,
                                 use_anchor=True))
        self.assertTrue(r["results"])
        hit = next((x for x in r["results"] if x["fact_id"] == self.fid), None)
        self.assertIsNotNone(hit, "anchor 通道未命中种子事实")
        self.assertTrue(hit["evidence"], "evidence 恒空 = 死腿复发")

    def test_direct_write_fact_has_empty_evidence_but_no_raise(self):
        """直写事实（无候选链）→ evidence 合法为空，且不抛（区分 empty≠error）。"""
        fid2 = _seed_fact(self.store, "一条没有任何证据链的直写事实")
        k = _kernel(self.store)
        self.assertEqual(k._evidence_for(fid2, top_n=2), [])


if __name__ == "__main__":
    unittest.main()
