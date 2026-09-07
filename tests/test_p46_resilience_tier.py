# -*- coding: utf-8 -*-
"""P46 韧性挡位 + 诚实遥测：检索通道三态断路器与 degraded 明示。

生产判例（P45 同族）：vector(chroma)/fts(graph.db) 任一后端异常时，
search()/trace() 直接 500——三通道无 per-channel 隔离，单后端故障
塌掉整个检索面。v14.1.0 P1 施工（借自 aduMEI v20.2 三态断路器）：

1. 通道隔离：单通道抛异常→该通道 0 命中+标记 degraded，其余通道
   （及锚通道/装配层）继续出结果，检索永不 500。
2. 诚实遥测：响应 channels 从「enabled」二元升级为三态——
   closed(健康)/degraded(本轮失败)/open(断路器跳闸已隔离)，
   顶层带 degraded 布尔，消费者可感知降级而非被 500 打脸。
3. 三态断路器：连续 FAILURE_THRESHOLD 次失败 → OPEN（跳过通道调用
   省资源）；OPEN 冷却 COOLDOWN_SECONDS 后进入 HALF_OPEN 放一次
   真实探测；探测成功回 CLOSED，失败回 OPEN 重新计冷却。

TDD RED → GREEN.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class _FlakyVector:
    """可编程炸/好的假 vector 后端：raise_after=True 时 query 抛异常。"""

    def __init__(self):
        self.fail = False
        self.calls = 0

    def query(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("chroma backend down (simulated)")
        return {"ids": [[]], "distances": [[]]}


class _FlakyFTS:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def search_ids(self, query, limit=50):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fts index locked (simulated)")
        return []


def _seed(store, content, *, fact_type, owner="mentor", domain="knowledge"):
    human_status = "unreviewed" if fact_type == "ephemeral" else "confirmed"
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status=human_status,
        ),
        actor_principal=owner,
    )["fact_id"]


class ResilienceFixture:
    """真 store + 锚通道事实 + 可编程炸的 vector/fts 后端。"""

    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.vector = _FlakyVector()
        self.fts = _FlakyFTS()
        # embedder 必须给出 vector 通道可用的形态（返回 list）
        self.kernel = QueryKernel(
            self.store, vector=self.vector, fts=self.fts, embedder=lambda text: [0.0] * 8
        )
        self.iron_id = _seed(self.store, "生产库只允许经 API 写入", fact_type="iron_rule")

    def search(self, **kw):
        defaults = dict(text="生产库 API 写入 铁律", principal_id="mentor", limit=20)
        defaults.update(kw)
        return self.kernel.search(QueryRequest(**defaults))

    def trace(self, **kw):
        defaults = dict(text="生产库 API 写入 铁律", principal_id="mentor", limit=20)
        defaults.update(kw)
        return self.kernel.trace(QueryRequest(**defaults))


class TestChannelIsolation(unittest.TestCase):
    """单通道异常不塌检索、其余通道继续出结果、响应诚实明示降级。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ResilienceFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_vector_failure_does_not_500_and_anchor_still_returns(self):
        """vector 炸 → 检索仍 200 且锚通道铁律照常返回 + degraded 明示。"""
        self.fx.vector.fail = True
        response = self.fx.search()
        self.assertIn(self.fx.iron_id, {r["fact_id"] for r in response["results"]},
                      "vector 炸时锚通道必须继续返回铁律（通道隔离）")
        self.assertTrue(response["degraded"], "顶层 degraded 必须为真（诚实遥测）")
        self.assertEqual(response["channels"]["vector"], "degraded")

    def test_fts_failure_degrades_only_fts(self):
        """fts 炸 → 只有 fts 标 degraded，vector 通道不受牵连。"""
        self.fx.fts.fail = True
        response = self.fx.search()
        self.assertEqual(response["channels"]["fts"], "degraded")
        self.assertEqual(response["channels"]["vector"], "closed")
        self.assertTrue(response["degraded"])

    def test_all_similar_channels_down_anchor_channel_survives(self):
        """vector+fts 双炸 → 锚通道独立存活（锚通道不依赖相似度后端）。"""
        self.fx.vector.fail = True
        self.fx.fts.fail = True
        response = self.fx.search()
        self.assertIn(self.fx.iron_id, {r["fact_id"] for r in response["results"]},
                      "三相似度通道全炸时锚通道必须独立存活")
        self.assertTrue(response["degraded"])

    def test_healthy_response_reports_closed_and_not_degraded(self):
        """全健康基线：channels 全 closed、degraded=False（防假阳性）。"""
        response = self.fx.search()
        self.assertFalse(response["degraded"])
        self.assertEqual(response["channels"]["vector"], "closed")
        self.assertEqual(response["channels"]["fts"], "closed")
        self.assertEqual(response["channels"]["anchor"], "closed")


class TestCircuitBreaker(unittest.TestCase):
    """三态断路器：连续失败跳闸 OPEN → 冷却后 HALF_OPEN 探测 → 回 CLOSED。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ResilienceFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_breaker_opens_after_consecutive_failures(self):
        """连续 N 次失败后 OPEN：后续检索直接跳过通道调用（省资源）。"""
        self.fx.vector.fail = True
        for _ in range(QueryKernel.FAILURE_THRESHOLD):
            self.fx.search()
        self.assertEqual(self.fx.kernel.breakers["vector"].state, "open",
                         f"连续 {QueryKernel.FAILURE_THRESHOLD} 次失败后断路器必须 OPEN")
        calls_before = self.fx.vector.calls
        self.fx.search()
        self.assertEqual(self.fx.vector.calls, calls_before,
                         "OPEN 态必须跳过通道调用（不再撞死马）")
        self.assertEqual(self.fx.search()["channels"]["vector"], "open")

    def test_breaker_half_open_probes_then_closes_on_success(self):
        """OPEN 冷却到期 → HALF_OPEN 放一次真实探测：成功回 CLOSED。"""
        self.fx.vector.fail = True
        for _ in range(QueryKernel.FAILURE_THRESHOLD):
            self.fx.search()
        breaker = self.fx.kernel.breakers["vector"]
        breaker.state = "half_open"  # 模拟冷却已过
        self.fx.vector.fail = False  # 后端恢复
        response = self.fx.search()
        self.assertEqual(breaker.state, "closed", "探测成功必须回 CLOSED")
        self.assertEqual(response["channels"]["vector"], "closed")

    def test_breaker_half_open_failed_probe_reopens(self):
        """HALF_OPEN 探测失败 → 回 OPEN 重新计冷却（不放行潮水流量）。"""
        self.fx.vector.fail = True
        for _ in range(QueryKernel.FAILURE_THRESHOLD):
            self.fx.search()
        breaker = self.fx.kernel.breakers["vector"]
        breaker.state = "half_open"
        self.fx.search()  # 探测仍失败
        self.assertEqual(breaker.state, "open", "探测失败必须回 OPEN")
        self.assertEqual(self.fx.search()["channels"]["vector"], "open")


class TestTraceMirrorsSearch(unittest.TestCase):
    """trace() 与 search() 同语义：通道隔离与 degraded 明示两口径不分叉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ResilienceFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_trace_channel_isolation_and_degraded(self):
        self.fx.vector.fail = True
        t = self.fx.trace()
        self.assertTrue(t["degraded"], "trace 顶层必须同带 degraded")
        self.assertEqual(t["channels"]["vector"], "degraded")


if __name__ == "__main__":
    unittest.main()
