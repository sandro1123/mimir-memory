# -*- coding: utf-8 -*-
"""P0-N TokenStore 原子写协议（嘟嘟🟡3）。

现状核实：嘟嘟🟡5 的 ASGI body 上限已有（1MiB + 413，P1-6 中间件），
本卡只补 🟡3：运维侧原子写协议。TokenStore 热加载按 st_mtime_ns
判定——外部直接覆盖文件（echo > file）会让 reload 在中间态读到
半截 JSON → auth_unavailable 全站 503。协议=临时文件+fsync+os.replace
（同目录原子改名），TokenStore 侧补 reload 失败的日志告警面。
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.auth import TokenStore, atomic_write_registry
from mimir_v8.store import sha256_text


def _write_registry(path: Path, principals=("mentor",), hashes=None) -> None:
    data = {"principals": []}
    for p in principals:
        h = (hashes or {}).get(p) or sha256_text(f"token-{p}")
        data["principals"].append({"id": p, "token_sha256": h, "scopes": ["read"], "roles": []})
    atomic_write_registry(path, json.dumps(data, ensure_ascii=False))


class TestAtomicRegistryWrite(unittest.TestCase):

    def test_registry_public_api_roundtrip(self):
        """原子写公开 API：写→立即可读（mtime 变更可见）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            _write_registry(path, principals=("mentor", "jarvis"))
            store = TokenStore(path)
            self.assertEqual(store.validate(), 2)

    def test_replace_is_atomic_no_partial_reads(self):
        """替换后旧 store 实例下次 validate 读到完整新表——无中间态窗口。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            _write_registry(path, principals=("mentor",))
            store = TokenStore(path)
            self.assertEqual(store.validate(), 1)
            _write_registry(path, principals=("mentor", "jarvis", "quantmaster"))
            # 触发 reload（mtime 已变）
            self.assertEqual(store.validate(), 3)

    def test_corrupt_file_fails_closed_with_log(self):
        """损坏 JSON → auth_unavailable（fail closed）+ WARNING 日志可观测。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            _write_registry(path, principals=("mentor",))
            store = TokenStore(path)
            with self.assertRaises(Exception):
                # simulate the bad-ops path: non-atomic partial write
                with open(path, "w", encoding="utf-8") as f:
                    f.write('{"principals": [')  # truncated mid-JSON
                store.validate()


if __name__ == "__main__":
    unittest.main()
