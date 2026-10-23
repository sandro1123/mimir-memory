# -*- coding: utf-8 -*-
"""抽取流水线修复：Assistant 回复中的技术实体能沉淀入库。

## 病根（2026-10-23 实测确认）

1. `worker.py` 拉上下文写死 `AND role='user'` —— Assistant 给出的方案、
   工具推荐、架构建议**完全进不了抽取模型**。
2. `evaluator._build_prompt` 只谈用户偏好/规则，Schema 也只允许单对象，
   技术实体（reference/learning）没有立足之地。
3. **需求里没提到、但会毁掉整个修复效果的一点**：`worker.py` 落候选时用
   `eval_result.summary or combined[:200]` 当内容。若摘要写成"用户询问
   NAS 视频压缩"，`Tdarr` 这个实体根本进不了事实——修了前面两处也白搭。

TDD RED：双向上下文与实体抽取尚不存在。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.evaluator import Evaluator

# 用户问一句，Assistant 给出具体工具 —— 典型的知识沉淀场景
USER_MSG = "我有台 NAS，想找能自动整理并压缩视频的软件，有推荐吗？"
ASSISTANT_MSG = (
    "推荐 Tdarr。它是一个开源的分布式转码/整理工具，可以对接 FFmpeg，"
    "按规则自动压缩与清理视频库，支持 Docker 部署，社区活跃。"
)


class TestBidirectionalContext(unittest.TestCase):
    """漏洞 1：上下文必须含 Assistant 回复。"""

    def test_assistant_role_is_included(self):
        from mimir_v8.worker import build_extraction_context

        msgs = [
            {"role": "user", "content_redacted": USER_MSG},
            {"role": "assistant", "content_redacted": ASSISTANT_MSG},
        ]
        ctx = build_extraction_context(msgs)
        self.assertIn("Tdarr", ctx, "Assistant 内容必须进入抽取上下文")
        self.assertIn(USER_MSG[:10], ctx, "用户提问也必须在")

    def test_roles_are_labeled(self):
        """必须标出谁说的——否则模型分不清用户诉求与助手建议。"""
        from mimir_v8.worker import build_extraction_context

        msgs = [
            {"role": "user", "content_redacted": USER_MSG},
            {"role": "assistant", "content_redacted": ASSISTANT_MSG},
        ]
        ctx = build_extraction_context(msgs)
        self.assertIn("[User]", ctx)
        self.assertIn("[Assistant]", ctx)

    def test_long_content_keeps_head_and_tail(self):
        """原有截断护栏必须保留（超长时保 head+tail，不是一刀切头）。"""
        from mimir_v8.worker import build_extraction_context

        # 头标记放在最前、尾标记放在最后——这才是 head+tail 能保留的形状。
        long_user = "HEADMARK " + "U" * 3000
        long_asst = "A" * 3000 + " TAILMARK"
        ctx = build_extraction_context([
            {"role": "user", "content_redacted": long_user},
            {"role": "assistant", "content_redacted": long_asst},
        ])
        self.assertLessEqual(len(ctx), 4100, "必须有上限，不得无限膨胀")
        self.assertIn("HEADMARK", ctx, "头部必须保留（含开场提问）")
        self.assertIn("TAILMARK", ctx, "尾部必须保留（结论常在末尾）")

    def test_empty_messages_yield_empty_context(self):
        from mimir_v8.worker import build_extraction_context

        self.assertEqual(build_extraction_context([]), "")


class TestPromptCoversTechnicalEntities(unittest.TestCase):
    """漏洞 2：提示词必须明确要求抽取技术实体。"""

    def test_prompt_mentions_reference_and_learning(self):
        e = Evaluator(api_key="test-key")
        text = " ".join(m["content"] for m in e._build_prompt("x"))
        self.assertIn("reference", text)
        self.assertIn("learning", text)

    def test_prompt_mentions_technical_entities(self):
        e = Evaluator(api_key="test-key")
        text = " ".join(m["content"] for m in e._build_prompt("x"))
        for kw in ("工具", "开源", "技术"):
            self.assertIn(kw, text, "提示词必须覆盖技术实体类知识：%s" % kw)

    def test_prompt_keeps_closed_sets(self):
        """闭集约束不得放宽（domain/fact_type 白名单照旧）。"""
        e = Evaluator(api_key="test-key")
        text = " ".join(m["content"] for m in e._build_prompt("x"))
        self.assertIn("infrastructure/quant/tech_support", text)
        self.assertIn("iron_rule/user_pref/project_config", text)

    def test_schema_still_single_object(self):
        """1.4.1 保持单对象输出——批量抽取是结构性改动，另立版本。"""
        e = Evaluator(api_key="test-key")
        text = " ".join(m["content"] for m in e._build_prompt("x"))
        self.assertIn('"is_valuable"', text)
        self.assertNotIn('"facts"', text, "1.4.1 不引入批量 schema")

    def test_injection_guard_unchanged(self):
        """输入注入防线不得削弱。"""
        from mimir_v8.evaluator import INPUT_INJECTION_PATTERNS

        self.assertTrue(INPUT_INJECTION_PATTERNS, "注入模式表不得为空")


class TestEntitySurvivesToFact(unittest.TestCase):
    """隐含缺口：实体必须真的进到事实内容里，不能只活在摘要里。"""

    def test_summary_missing_entity_falls_back_to_content(self):
        """摘要没提实体时，事实内容必须回退到原文（含 Tdarr），
        否则「修好了抽取」但「入库的还是不含 Tdarr 的摘要」。"""
        from mimir_v8.worker import resolve_fact_content

        content = build_ctx = None
        msgs = [
            {"role": "user", "content_redacted": USER_MSG},
            {"role": "assistant", "content_redacted": ASSISTANT_MSG},
        ]
        from mimir_v8.worker import build_extraction_context
        content = build_extraction_context(msgs)

        # 摘要丢了实体
        fact_content = resolve_fact_content("用户询问 NAS 视频压缩软件", content)
        self.assertIn("Tdarr", fact_content,
                      "摘要未包含实体时必须回退到含实体的原文")

    def test_good_summary_is_kept(self):
        from mimir_v8.worker import resolve_fact_content

        fact_content = resolve_fact_content(
            "Tdarr 是开源分布式转码工具，可对接 FFmpeg 自动压缩整理视频库",
            "原始长文 Tdarr " + "x" * 500)
        self.assertTrue(fact_content.startswith("Tdarr 是开源"),
                        "好摘要应当保留，不被无谓替换")


class TestSourceGuardsTheSQL(unittest.TestCase):
    """钉住**源码里的那条 SQL**。

    教训（本卡实测踩中）：端到端测试若自己重写一遍 SQL 字符串，把 bug
    改回去测试仍然全绿——它测的是测试自己，不是生产代码（判例 #150
    「注掉接线仍然绿」同族）。所以必须直接检查生产源码里那句查询。
    """

    def test_extraction_sql_is_bidirectional(self):
        import inspect
        from mimir_v8 import worker

        src = inspect.getsource(worker.llm_extract_once)
        self.assertIn("conversation_messages", src)
        self.assertNotIn(
            "role='user'", src,
            "抽取上下文不得再写死 role='user'——Assistant 的方案/工具推荐会被丢弃")
        self.assertIn("'user','assistant'", src.replace(" ", ""),
                      "必须同时拉取 user 与 assistant")

    def test_uses_build_extraction_context(self):
        import inspect
        from mimir_v8 import worker

        src = inspect.getsource(worker.llm_extract_once)
        self.assertIn("build_extraction_context(", src,
                      "必须走统一的拼接函数，否则角色标签与截断护栏会分叉")


class TestEndToEndSQL(unittest.TestCase):
    """端到端：真实库 + 真实 SQL，确认 assistant 消息真被拉出来。

    只测纯函数不足以证明修好了——病根在 SQL 那句 `role='user'`。
    """

    def test_sql_pulls_assistant_rows(self):
        """走真实入口 ingest_conversation 写入，再用真实 SQL 拉取。"""
        import tempfile
        from pathlib import Path
        from mimir_v8.learning import (ConversationEnvelope,
                                       ConversationMessage, LearningService)
        from mimir_v8.store import CanonicalStore
        from mimir_v8.worker import build_extraction_context

        tmp = tempfile.mkdtemp()
        store = CanonicalStore(Path(tmp) / "canon.db")
        svc = LearningService(store)
        env = ConversationEnvelope(
            connector_type="hermes",
            connector_id="c-e2e",
            session_id="s-e2e",
            owner_principal="mentor",
            memory_mode="observe",
            retention_class="standard",
            messages=(
                ConversationMessage(role="user", content=USER_MSG),
                ConversationMessage(role="assistant", content=ASSISTANT_MSG),
            ),
            idempotency_key="e2e-tdarr",
        )
        out = svc.ingest_conversation(env, actor_principal="mentor")
        source_id = out["source_id"]

        with store.connect() as conn:
            msgs = [dict(m) for m in conn.execute(
                "SELECT role, content_redacted FROM conversation_messages"
                " WHERE source_id=? AND role IN ('user','assistant')"
                " ORDER BY ordinal LIMIT 20", (source_id,)).fetchall()]

        ctx = build_extraction_context(msgs)
        roles = {m["role"] for m in msgs}
        self.assertEqual(roles, {"user", "assistant"},
                         "两个角色都必须被 SQL 拉出来（病根就在这句 SQL）")
        self.assertIn("Tdarr", ctx)


if __name__ == "__main__":
    unittest.main()
