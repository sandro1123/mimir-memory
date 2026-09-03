"""Mímir v14.0 — /v14/projection REST 端点（跨模型投影 API 面）.

projection.project_context 是纯库函数；本件把它接上检索面：
POST /v14/projection {text, tier, limit?} → QueryKernel.search →
project_context，一次调用给出「这一问、这一档模型」的注入块。

REST 面守门铁律（与 /v14/skills/* 同门）：
- read scope 门：ingest-only token 403；
- tier 必须在 MODEL_TIERS，未知档 422 Fail-Closed；
- text 必填，空文本 422（投影空面无意义）；
- ProjectionError 一律 422，不落 500。

工程四严律：TDD 先行（本文件 RED 先跑）/ 投影纯函数不落事件流 /
无路径操作 / 全量回归门禁。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.auth import TokenStore
from mimir_v8.projection import MODEL_TIERS
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _fact(
    store,
    content,
    *,
    owner="mentor",
    fact_type="pattern",
    visibility="all",
    domain="knowledge",
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
            human_status="confirmed",
        ),
        actor_principal=owner,
    )["fact_id"]


class TestProjectionAPI(unittest.TestCase):
    """/v14/projection — REST 面：scope 门 + Fail-Closed 校验。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app

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

    def _hdr(self, token="tok-mentor"):
        return {"Authorization": f"Bearer {token}"}

    # ── happy path: search → project, full contract shape ───────────

    def test_projection_endpoint_happy_path(self):
        _fact(self.store, "铁律：写库必带幂等键", fact_type="iron_rule")
        _fact(self.store, "pattern：先看慢查询再查连接泄漏",
              fact_type="pattern")
        r = self.client.post(
            "/v14/projection",
            json={"text": "幂等键", "tier": "claude", "limit": 10},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["tier"], "claude")
        self.assertEqual(body["dialect"], "markdown")
        self.assertEqual(body["budget"], MODEL_TIERS["claude"]["max_tokens"])
        self.assertGreaterEqual(body["estimated_tokens"], 1)
        self.assertGreaterEqual(len(body["blocks"]), 1)
        layers = [b["layer"] for b in body["blocks"]]
        # layer-first ordering: all 3s before any lower layer
        self.assertEqual(layers, sorted(layers, reverse=True))
        for block in body["blocks"]:
            self.assertIn("fact_id", block)
            self.assertIn("fact_type", block)
            self.assertTrue(block["text"])

    def test_projection_tier_dialects_differ(self):
        _fact(self.store, "偏好：回复用中文，代码注释也是", fact_type="user_pref")
        texts = {}
        for tier in ("claude", "deepseek", "local-small"):
            r = self.client.post(
                "/v14/projection",
                json={"text": "回复用中文的偏好", "tier": tier, "limit": 10},
                headers=self._hdr(),
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["tier"], tier)
            self.assertEqual(body["dialect"],
                             MODEL_TIERS[tier]["dialect"])
            texts[tier] = [b["text"] for b in body["blocks"]]
        # same L3 fact, three dialects → three different renderings
        self.assertEqual(len(texts["claude"]), len(texts["deepseek"]),
                         len(texts["local-small"]))
        self.assertNotEqual(texts["claude"], texts["local-small"])

    def test_projection_l3_survives_compact_tier(self):
        content = "技能：数据库排障五步法，第一步查锁等待"
        _fact(self.store, content, fact_type="skill")
        r = self.client.post(
            "/v14/projection",
            json={"text": "数据库排障五步法", "tier": "local-small",
                  "limit": 10},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        joined = "\n".join(b["text"] for b in r.json()["blocks"])
        self.assertIn(content, joined)  # L3 verbatim, any tier

    # ── validation: Fail-Closed, never a 500 ─────────────────────────

    def test_projection_unknown_tier_422(self):
        r = self.client.post(
            "/v14/projection",
            json={"text": "x", "tier": "gpt-mega"},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_projection_missing_text_422(self):
        r = self.client.post(
            "/v14/projection",
            json={"tier": "claude"},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_projection_empty_results_project_to_empty_blocks(self):
        r = self.client.post(
            "/v14/projection",
            json={"text": "zzz-no-such-topic-qqq", "tier": "claude"},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["blocks"], [])
        self.assertEqual(body["estimated_tokens"], 0)

    # ── scope gate: read required, ingest-only 403 ───────────────────

    def test_projection_requires_read_scope(self):
        r = self.client.post(
            "/v14/projection",
            json={"text": "x", "tier": "claude"},
            headers=self._hdr("tok-ingest"),
        )
        self.assertEqual(r.status_code, 403, r.text)

    def test_projection_requires_authentication(self):
        r = self.client.post(
            "/v14/projection", json={"text": "x", "tier": "claude"}
        )
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
