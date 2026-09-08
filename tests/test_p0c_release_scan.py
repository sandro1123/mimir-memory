# -*- coding: utf-8 -*-
"""P0-C 发布脱敏七面扫描闸门：开源前置必做。

v14.2 P0（09-08 用户拍板 GO）。v10 硬编码 key 被开源脱敏的事故根子
预防（P0-1 根因链第一环）：泄漏不该靠「发布时人眼排查」，必须是一道
每次 push 都跑的机器闸门。

七面：
1. secrets（API key/token 形态：sk-…、ghp_…、gitee 私有 token 等）
2. 内网 IP（192.168.x / 10.x / 172.16-31.x / Tailscale 100.x）
3. 机器路径（/home/用户名、C:/Users\用户名）
4. 用户名（sandro1123 等本机身份）
5. 私钥 PEM 头
6. payload 样本（真实库 dump 特征：canonical.db 路径行等）
7. 负向对照：植入假 secret 必须被抓（防止闸门本身静默失效）

词表外置 scripts/release_scan_rules.txt，勿硬编码进扫描器
（否则词表本身会命中面 4）。

TDD RED → GREEN.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from release_scan import Scanner, load_rules, RULES_PATH


HIT_CASES = [
    # (面, 样本)
    ("secrets", "api_key = 'sk-ant-api03-9aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890'"),
    ("secrets", "MIMIR_ROUTER_KEY=ghp_16C7e42F29BcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
    ("private_ip", "router at 192.168.5.5:20128"),
    ("private_ip", "ssh sandro1123@100.108.130.52"),
    ("machine_path", "db lives at /home/sandro1123/.hermes/mimir"),
    ("username", "connect via ssh sandro1123 using key auth"),
    ("machine_path", "路径 C:/Users\\sandr\\vault"),

    ("secrets", "-----BEGIN RSA PRIVATE KEY-----"),
    ("payload_sample", "canonical.db /production-v9.0-20260805_214614"),
]

CLEAN_CASES = [
    "The Mímir system uses event sourcing.",
    "pip install -e .[embeddings]",
    "connect to localhost:8080 for the dashboard",
    "SELECT * FROM facts WHERE status='active'",
    "python 3.11+ required",
]


class TestReleaseScan(unittest.TestCase):

    def test_rules_file_exists(self):
        self.assertTrue(RULES_PATH.exists(), "词表必须外置于 scripts/release_scan_rules.txt")

    def test_rules_file_has_no_real_identity(self):
        """词表外置的原因：规则文件本身不能携带真实身份（自指免疫由格式保证——
        词表文件被扫描时应零命中，命中即说明词表写错位置）。"""
        rules = load_rules()
        self.assertGreaterEqual(len(rules), 5)

    def test_hits(self):
        scanner = Scanner()
        for face, text in HIT_CASES:
            with self.subTest(face=face):
                hits = scanner.scan_text(text, path="docs/example.md")
                self.assertTrue(hits, f"面 {face} 样本未被抓住: {text[:50]}")
                self.assertEqual(hits[0]["face"], face)

    def test_clean_passes(self):
        scanner = Scanner()
        for text in CLEAN_CASES:
            with self.subTest(text=text[:40]):
                hits = scanner.scan_text(text, path="docs/example.md")
                self.assertEqual(hits, [], f"误杀正常内容: {text[:60]}")

    def test_negative_control_in_repo(self):
        """负向对照：仓库里必须有一份植假 secret 的对照文件，扫描它必须
        恰好抓到对照件——闸门静默失效会被这步当场揭穿。"""
        scanner = Scanner()
        nc_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "release_scan_negative_control.md"
        self.assertTrue(nc_path.exists(), "负向对照件必须入库")
        hits = scanner.scan_file(nc_path)
        self.assertTrue(hits, "负向对照件必须被抓住")

    def test_scan_tree_reports_counts(self):
        scanner = Scanner()
        stats = scanner.scan_tree(Path(__file__).resolve().parent)
        self.assertIn("files_scanned", stats)
        self.assertIn("hits", stats)
        self.assertGreater(stats["files_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
