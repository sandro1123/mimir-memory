# -*- coding: utf-8 -*-
"""roadmap #13：三段式渐进披露的签名游标。

HMAC 签名提供完整性，不提供机密性；展开必须绑定主体并重新检查 ACL。
跨步拒绝：**任意 ID**、**改范围**、**过期 token**、**不同主体**。

TDD RED -> GREEN。

设计选择（#13 讨论结论）：老端点加 `detail=index|full`，**默认 full**
（零破坏）。游标使用 HMAC 签名并由调用方持有；签名不提供载荷机密性。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.recall_cursor import (
    CursorError,
    CursorExpired,
    CursorScopeMismatch,
    CursorSignatureInvalid,
    mint_cursor,
    verify_cursor,
)

SECRET = b"test-secret-not-for-production"


class TestCursorRoundTrip(unittest.TestCase):
    """签发 -> 验证 必须能还原出足以驱动下一步的状态。"""

    def test_mint_then_verify_returns_payload(self):
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=10,
                          issued_refs=["r1", "r2"], ttl_seconds=300)
        payload = verify_cursor(SECRET, tok, scope="q-abc", principal_id="mentor")
        self.assertEqual(payload["principal_id"], "mentor")
        self.assertEqual(payload["offset"], 10)
        self.assertEqual(payload["issued_refs"], ["r1", "r2"])

    def test_cursor_is_bound_to_principal(self):
        from mimir_v8.recall_cursor import CursorPrincipalMismatch
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=0,
                          issued_refs=["r1"], ttl_seconds=300)
        with self.assertRaises(CursorPrincipalMismatch):
            verify_cursor(SECRET, tok, scope="q-abc", principal_id="other")

    def test_cursor_is_opaque_string(self):
        """游标是字符串，且**不得**含明文的内部 fact_id。"""
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=0,
                          issued_refs=["fact-1234-secret-id"], ttl_seconds=300)
        self.assertIsInstance(tok, str)
        self.assertNotIn("fact-1234-secret-id", tok,
                         "游标不得下发内部 ID 明文")


class TestCursorRejections(unittest.TestCase):
    """三种必拒形态（issue #7 明列）。"""

    def test_tampered_token_is_rejected(self):
        """改过签名的 token —— 等价于伪造。"""
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=5,
                          issued_refs=["r1"], ttl_seconds=300)
        tampered = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
        with self.assertRaises(CursorSignatureInvalid):
            verify_cursor(SECRET, tampered, scope="q-abc")

    def test_wrong_secret_is_rejected(self):
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=5,
                          issued_refs=["r1"], ttl_seconds=300)
        with self.assertRaises(CursorSignatureInvalid):
            verify_cursor(b"other-secret", tok, scope="q-abc")

    def test_scope_mismatch_is_rejected(self):
        """改范围：拿 A 查询的游标去续 B 查询。"""
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=5,
                          issued_refs=["r1"], ttl_seconds=300)
        with self.assertRaises(CursorScopeMismatch):
            verify_cursor(SECRET, tok, scope="q-DIFFERENT")

    def test_expired_token_is_rejected(self):
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=5,
                          issued_refs=["r1"], ttl_seconds=-1)  # 立刻过期
        with self.assertRaises(CursorExpired):
            verify_cursor(SECRET, tok, scope="q-abc")

    def test_all_rejections_share_a_common_base(self):
        """三种拒绝同族——调用方可统一 except CursorError。"""
        for exc in (CursorSignatureInvalid, CursorScopeMismatch, CursorExpired):
            self.assertTrue(issubclass(exc, CursorError))


class TestIssuedRefsGuard(unittest.TestCase):
    """'拒绝任意 ID'：只能展开**本次召回发放过**的 ref。"""

    def test_payload_carries_only_issued_refs(self):
        tok = mint_cursor(SECRET, scope="q-abc", principal_id="mentor", offset=0,
                          issued_refs=["r1", "r2"], ttl_seconds=300)
        payload = verify_cursor(SECRET, tok, scope="q-abc")
        self.assertEqual(set(payload["issued_refs"]), {"r1", "r2"})


if __name__ == "__main__":
    unittest.main()
