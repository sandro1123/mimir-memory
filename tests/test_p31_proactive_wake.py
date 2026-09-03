"""Mímir v13.0 — 主动意图预测性前置唤醒 (spec 阶段三任务3).

Agent 在接收到任务指令的前置阶段，Mímir 根据上下文意图与风险特征，
主动推送历史避坑指南与强约束规则——无需 Agent 被动发起查询。

语义分层：
- 铁律 (iron_rule) 与核心偏好 (user_pref) 是安全底线：任何意图下
  永远推送（与检索锚通道 ANCHOR_FACT_TYPES 同一语义）——
  「系统安全底线永不丢」。
- L2 pattern（历史排障经验沉淀）按意图关键词命中推送：
  排障意图 → 推送排障 pattern；变更意图 → 推送变更前检查 pattern。
  无关键词命中时不推送 pattern（避免噪声，铁律仍推）。
- 意图分类轻实现可测不依赖 LLM：关键词驱动的 IntentProfiler。

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.relevance import IntentProfiler, ProactiveWake
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _fact(
    store,
    content,
    *,
    owner="mentor",
    fact_type="pattern",
    visibility="all",
):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain="knowledge",
            fact_type=fact_type,
            visibility=visibility,
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
        ),
        actor_principal=owner,
    )["fact_id"]


class TestIntentProfiler(unittest.TestCase):
    """意图分类：关键词驱动，轻实现可测不依赖 LLM。"""

    def test_troubleshooting_intent(self):
        self.assertEqual(
            IntentProfiler.classify("帮我看下为什么 pod 一直 crash"),
            "troubleshooting",
        )
        self.assertEqual(IntentProfiler.classify("数据库连接超时怎么排查"), "troubleshooting")

    def test_change_intent(self):
        self.assertEqual(
            IntentProfiler.classify("把生产库的备份策略改成增量"), "change"
        )
        self.assertEqual(IntentProfiler.classify("更新 k8s 节点配置"), "change")

    def test_destructive_intent(self):
        self.assertEqual(IntentProfiler.classify("删除旧的索引文件"), "destructive")
        self.assertEqual(IntentProfiler.classify("drop table 恢复演练"), "destructive")

    def test_generic_intent(self):
        self.assertEqual(IntentProfiler.classify("写一份周报总结"), "generic")
        self.assertEqual(IntentProfiler.classify(""), "generic")

    def test_risk_flag_on_destructive(self):
        profile = IntentProfiler.profile("清空缓存目录后删除旧备份")
        self.assertEqual(profile["intent"], "destructive")
        self.assertTrue(profile["risky"])
        # generic 且不撞破坏性关键词 → 不标险
        plain = IntentProfiler.profile("整理本周会议纪要")
        self.assertFalse(plain["risky"])


class TestProactiveWake(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CanonicalStore(self.root / "canonical.db")
        self.wake = ProactiveWake(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_iron_rules_always_pushed(self):
        """铁律永远推送——任何意图，安全底线永不丢。"""
        rid = _fact(
            self.store, "禁止在非变更窗口执行生产 SQL", fact_type="iron_rule"
        )
        result = self.wake.wake("写一份周报总结", principal_id="mentor")
        self.assertTrue(any(m["fact_id"] == rid for m in result["iron_rules"]))

    def test_user_prefs_always_pushed(self):
        pid = _fact(
            self.store, "用户偏好：报告一律用中文", fact_type="user_pref"
        )
        result = self.wake.wake("写一份周报总结", principal_id="mentor")
        self.assertTrue(any(m["fact_id"] == pid for m in result["user_prefs"]))



class TestProactiveWakePatterns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CanonicalStore(self.root / "canonical.db")
        self.wake = ProactiveWake(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pattern_pushed_on_matching_intent(self):
        """排障意图 → 命中关键词的排障 pattern 被推送。"""
        pid = _fact(
            self.store, "K8s 节点漂移排障：先 drain 再查 cgroup"
        )
        result = self.wake.wake("帮我看下为什么 pod 一直 crash", principal_id="mentor")
        self.assertTrue(any(m["fact_id"] == pid for m in result["patterns"]))

    def test_pattern_not_pushed_when_no_keyword_match(self):
        """无关键词命中 → pattern 不推送（避免噪声，铁律仍推）。"""
        pid = _fact(
            self.store, "K8s 节点漂移排障：先 drain 再查 cgroup"
        )
        result = self.wake.wake("写一份周报总结", principal_id="mentor")
        self.assertEqual([m["fact_id"] for m in result["patterns"]], [])

    def test_pattern_match_is_case_insensitive(self):
        pid = _fact(
            self.store, "K8S POD CrashLoop 历史排障记录"
        )
        result = self.wake.wake("帮我看下为什么 pod 一直 crash", principal_id="mentor")
        fact = self.store.get_fact(pid)
        self.assertTrue(
            any(m["fact_id"] == pid for m in result["patterns"]),
            f"content={fact['content']!r}",
        )

    def test_acl_owner_only_pattern_not_pushed_to_other_principal(self):
        """ACL：owner_only pattern 对非 owner 不推送。"""
        pid = _fact(
            self.store,
            "私密排障笔记：生产凭据轮换流程",
            owner="heimdallr",
            visibility="owner_only",
        )
        result = self.wake.wake(
            "帮我看下为什么 pod 一直 crash", principal_id="mentor"
        )
        self.assertNotIn(pid, [m["fact_id"] for m in result["patterns"]])

    def test_result_shape_and_profiles_included(self):
        rid = _fact(
            self.store, "禁止在非变更窗口执行生产 SQL", fact_type="iron_rule"
        )
        result = self.wake.wake("删除旧的索引文件", principal_id="mentor")
        self.assertEqual(result["intent"], "destructive")
        self.assertTrue(result["risky"])
        self.assertIn("matched_keywords", result["profile"])
        self.assertEqual(result["iron_rules"][0]["fact_id"], rid)
        self.assertEqual(
            set(result.keys()), {"intent", "risky", "profile", "iron_rules", "user_prefs", "patterns"}
        )


class TestProactiveWakeAPI(unittest.TestCase):
    """/v13/wake — REST 面：scope 门与身份不可伪造。"""

    def setUp(self):
        import hashlib
        import json

        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.query import QueryKernel
        from mimir_v8.relevance import ProactiveWake

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canonical.db")
        self.wake = ProactiveWake(self.store)
        token_path = root / "tokens.json"
        tokens = {"mentor": "tok-mentor"}
        token_path.write_text(
            json.dumps({
                "principals": [{
                    "id": principal,
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "scopes": ["read", "write"],
                    "roles": [],
                    "admin": False,
                } for principal, token in tokens.items()]
            }),
            encoding="utf-8",
        )
        self.client = TestClient(
            create_app(
                ServiceContext(
                    store=self.store,
                    token_store=TokenStore(token_path),
                    query=QueryKernel(self.store),
                    wake=self.wake,
                )
            ),
            raise_server_exceptions=False,
        )
        self.headers = {"Authorization": "Bearer tok-mentor"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_wake_endpoint(self):
        rid = _fact(
            self.store, "禁止在非变更窗口执行生产 SQL", fact_type="iron_rule"
        )
        r = self.client.post(
            "/v13/wake", json={"text": "删除旧的索引文件"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["intent"], "destructive")
        self.assertTrue(any(m["fact_id"] == rid for m in data["iron_rules"]))

    def test_wake_requires_authentication(self):
        r = self.client.post("/v13/wake", json={"text": "查一下"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_wake_requires_read_scope(self):
        """ingest-only token 不可用 wake（读面）。"""
        import hashlib
        import json

        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.query import QueryKernel
        from mimir_v8.relevance import ProactiveWake

        self._tmp2 = tempfile.TemporaryDirectory()
        root = Path(self._tmp2.name)
        store = CanonicalStore(root / "canonical.db")
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [{
                    "id": "ingestor",
                    "token_sha256": hashlib.sha256(b"tok-ingest").hexdigest(),
                    "scopes": ["ingest"],
                    "roles": [],
                    "admin": False,
                }]
            }),
            encoding="utf-8",
        )
        client = TestClient(
            create_app(
                ServiceContext(
                    store=store,
                    token_store=TokenStore(token_path),
                    query=QueryKernel(store),
                    wake=ProactiveWake(store),
                )
            ),
            raise_server_exceptions=False,
        )
        r = client.post(
            "/v13/wake", json={"text": "查一下"},
            headers={"Authorization": "Bearer tok-ingest"},
        )
        self.assertEqual(r.status_code, 403, r.text)


class TestProactiveWakeRuntimeWiring(unittest.TestCase):
    """runtime 组装守卫：build_runtime 必须构造并注入 ProactiveWake。

    v14 部署验收抓到的「建了没通电」判例：库/REST 面/测试全在，
    但 runtime.build_runtime 从未构造 ProactiveWake —— ServiceContext.wake
    恒为 None，/v13/wake 在生产永远 503。此前测试手动塞 wake=self.wake
    才绿，掩盖了组装缺口。本测试直接断言真实组装路径（不手动塞参），
    防止未来再次断线。
    """

    def test_build_runtime_wires_proactive_wake(self):
        import hashlib
        import json

        from mimir_v8.runtime import build_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "runtime-wake-token"
            token_path = root / "tokens.json"
            token_path.write_text(
                json.dumps({"principals": [{
                    "id": "mentor",
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "scopes": ["read", "write"],
                    "roles": [],
                    "admin": False,
                }]}),
                encoding="utf-8",
            )
            app, _components = build_runtime(
                root / "data",
                token_path,
                vector_enabled=False,
                start_supervisor=False,
            )
            context = app.state.context
            self.assertIsNotNone(context.wake)
            from mimir_v8.relevance import ProactiveWake
            self.assertIsInstance(context.wake, ProactiveWake)
            # v14 部署验收抓到的第三处同病：graph 投影器建了（供检索
            # 内核用）但从不传 ServiceContext —— /v13/graph/* 生产 503。
            self.assertIsNotNone(context.graph)
            from mimir_v8.graph_projector import GraphProjector
            self.assertIsInstance(context.graph, GraphProjector)

            # 端到端：组装出的 app 对 /v13/wake 与 /v13/graph/history
            # 真实返回 200 而非 503（空库 → history 为空列表，非报错）。
            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                r = client.post(
                    "/v13/wake", json={"text": "写一份周报总结"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["intent"], "generic")

                r2 = client.get(
                    "/v13/graph/history",
                    params={"entity_id": "auto_synced"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(r2.status_code, 200, r2.text)
                self.assertIn("edges", r2.json())


if __name__ == "__main__":
    unittest.main()
