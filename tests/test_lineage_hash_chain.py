# -*- coding: utf-8 -*-
"""1.1.0 Task 1 谱系哈希链 — fact_versions 链式咬合 + verify 对账。

链定义：第 n 版的 previous_version_hash = lineage_hash(n-1)，其中
lineage_hash(n) = sha256(f"{prev_lineage or ''}:{snapshot_json(canonical)}")。
genesis（v1）prev 为空串。verify 逐版本重算比对，任何一环断裂即报。

两层防线（各自独立可测）：
1. 既有 immutability 触发器 —— 正常 SQL 路径改不动历史
2. 本卡的哈希链 —— 带外拿到文件、DROP TRIGGER 后改历史，链值仍能指认

三态判语（对标 aduMEI v20.5 密码学谱系，Roadmap 差距#6）：sealed_ok /
broken / unsealed。**unsealed 不是「完好」而是「无从验证」**——1.1.0
之前的旧事实无封存链接，必须显式披露，不得静默输出零（绿灯不制造虚假
信心）。不可变表刻意不倒填：回填即篡改历史。

TDD RED → GREEN.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import CreateFact, UpdateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content="谱系链测试事实"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal="mentor",
        fact_type="event", domain="system", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal="mentor")["fact_id"]


def _update(store, fact_id, content, expected_version):
    return store.update_fact(UpdateFact(
        fact_id=fact_id, content=content, expected_version=expected_version,
        change_reason="谱系链测试",
    ), actor_principal="mentor")


class TestLineageChain(unittest.TestCase):

    def test_genesis_version_has_null_prev(self):
        """v1 是链首，previous_version_hash 为空（NULL 或空串）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            with store.connect() as c:
                row = c.execute(
                    "SELECT previous_version_hash FROM fact_versions "
                    "WHERE fact_id=? AND version=1", (fid,)).fetchone()
            self.assertIn(row["previous_version_hash"], (None, ""))

    def test_new_version_carries_previous_hash(self):
        """v2.previous_version_hash == v1 的 lineage hash。"""
        from mimir_v8.lineage import compute_lineage_hash

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            with store.connect() as c:
                v1 = c.execute(
                    "SELECT snapshot_json, previous_version_hash FROM fact_versions "
                    "WHERE fact_id=? AND version=1", (fid,)).fetchone()
                v1_snapshot, v1_prev = v1["snapshot_json"], v1["previous_version_hash"]
            _update(store, fid, "第二版内容", 1)
            with store.connect() as c:
                v2 = c.execute(
                    "SELECT previous_version_hash FROM fact_versions "
                    "WHERE fact_id=? AND version=2", (fid,)).fetchone()
            self.assertEqual(
                v2["previous_version_hash"],
                compute_lineage_hash(v1_prev, v1_snapshot),
            )

    def test_three_version_chain_is_linked(self):
        """v3 链到 v2 的 lineage hash，逐环咬合（不止首环）。"""
        from mimir_v8.lineage import compute_lineage_hash

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            _update(store, fid, "第二版内容", 1)
            with store.connect() as c:
                v2 = c.execute(
                    "SELECT snapshot_json, previous_version_hash FROM fact_versions "
                    "WHERE fact_id=? AND version=2", (fid,)).fetchone()
                v2_snapshot, v2_prev = v2["snapshot_json"], v2["previous_version_hash"]
            _update(store, fid, "第三版内容", 2)
            with store.connect() as c:
                v3 = c.execute(
                    "SELECT previous_version_hash FROM fact_versions "
                    "WHERE fact_id=? AND version=3", (fid,)).fetchone()
            self.assertEqual(
                v3["previous_version_hash"],
                compute_lineage_hash(v2_prev, v2_snapshot),
            )

    def test_verify_chain_intact(self):
        from mimir_v8.lineage import verify_lineage_chain

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            _update(store, fid, "第二版内容", 1)
            report = verify_lineage_chain(store, fid)
            self.assertTrue(report["chain_intact"])
            self.assertEqual(report["breaks"], [])
            self.assertEqual(report["fact_id"], fid)

    def test_immutability_trigger_blocks_inline_tamper(self):
        """第一道防线：正常 SQL 路径下事实版本不可改（既有触发器）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            with store.connect() as c:
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(
                        "UPDATE fact_versions SET snapshot_json='{}' "
                        "WHERE fact_id=? AND version=1", (fid,))

    def test_verify_detects_out_of_band_tamper(self):
        """第二道防线（本卡）：攻击者带外拿到文件、DROP TRIGGER 后改历史。

        这正是哈希链存在的理由——触发器挡不住文件级访问，链值能。
        """
        from mimir_v8.lineage import verify_lineage_chain

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            _update(store, fid, "第二版内容", 1)
            with store.connect() as c:
                # 模拟带外攻击：绕开防线（触发器）后篡改第一版快照
                c.execute("DROP TRIGGER IF EXISTS fact_versions_no_update")
                row = c.execute(
                    "SELECT snapshot_json FROM fact_versions "
                    "WHERE fact_id=? AND version=1", (fid,)).fetchone()
                tampered = json.loads(row["snapshot_json"])
                tampered["content"] = "伪造内容"
                c.execute(
                    "UPDATE fact_versions SET snapshot_json=? "
                    "WHERE fact_id=? AND version=1",
                    (json.dumps(tampered, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")), fid),
                )
            report = verify_lineage_chain(store, fid)
            self.assertEqual(report["chain_status"], "broken")
            self.assertFalse(report["chain_intact"])
            self.assertIn(2, report["breaks"])

    def test_verify_reports_unsealed_for_legacy_facts(self):
        """旧事实（无封存链接）返回 unsealed——空集显式报警，不得静默输出完好。"""
        from mimir_v8.lineage import verify_lineage_chain

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            _update(store, fid, "第二版内容", 1)
            with store.connect() as c:
                # 模拟 1.1.0 之前建的库：清掉全部链值
                c.execute("DROP TRIGGER IF EXISTS fact_versions_no_update")
                c.execute(
                    "UPDATE fact_versions SET previous_version_hash=NULL "
                    "WHERE fact_id=?", (fid,))
            report = verify_lineage_chain(store, fid)
            self.assertEqual(report["chain_status"], "unsealed")
            self.assertFalse(report["chain_intact"])
            self.assertEqual(report["sealed_links"], 0)

    def test_unsealed_prefix_then_sealed_tail(self):
        """混合态：旧版无链值（未封存前缀）+ 新版有链值 → 尾段咬合即 sealed_ok。"""
        from mimir_v8.lineage import verify_lineage_chain

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            _update(store, fid, "第二版内容", 1)
            with store.connect() as c:
                c.execute("DROP TRIGGER IF EXISTS fact_versions_no_update")
                c.execute(
                    "UPDATE fact_versions SET previous_version_hash=NULL "
                    "WHERE fact_id=? AND version=2", (fid,))
            # 第三版由正常写路径补链（前驱 v2 链值为 NULL → 按空串参与）
            _update(store, fid, "第三版内容", 2)
            report = verify_lineage_chain(store, fid)
            self.assertEqual(report["chain_status"], "sealed_ok")
            self.assertEqual(report["breaks"], [])
            self.assertEqual(report["sealed_links"], 1)


if __name__ == "__main__":
    unittest.main()
