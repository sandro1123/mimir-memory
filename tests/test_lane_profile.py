# -*- coding: utf-8 -*-
"""引擎挡位一等化（LaneProfile）三钉 — 1.2.0 卡一。

卡一病机：挡位此前是「请求字段 × 接线状态」隐式算出来的，而跑分报告里那句
`"fts-only trigram + RRF (no vector lane)"` 是人手写的散文。接上向量道后报告
会继续写「no vector lane」——数字对、描述错，且永远不会有人发现。

**反向钉是本卡的核心守卫**：请求了向量道而给不了时，必须显式失败；
悄悄退回 fts-only 出数 = 报告里的描述和数字来自两套引擎。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mimir_v8.query import (  # noqa: E402
    LANE_WIRING, SIMILARITY_LANES, LaneProfile, QueryKernel, QueryRequest)


class _StubVector:
    """只需满足接线存在性；不参与真实检索（LaneProfile 只看属性非 None）。"""

    def query(self, **kwargs):
        return {"ids": [[]], "distances": [[]]}


class _StubLane:
    def search_ids(self, query, limit=10):
        return []


def _kernel(**lanes):
    kwargs = {}
    for lane in lanes:
        if lane == "vector":
            kwargs["vector"] = _StubVector()
            kwargs["embedder"] = lambda t: [0.0] * 4
        else:
            kwargs[lane] = _StubLane()
    return QueryKernel(store=None, **kwargs)


def _req(**over):
    base = dict(text="q", principal_id="p", limit=10)
    base.update(over)
    return QueryRequest(**base)


class TestLaneProfile(unittest.TestCase):
    # ── 正向 ────────────────────────────────────────────────
    def test_all_lanes_wired_and_requested_reports_them_all(self):
        kernel = _kernel(vector=True, fts=True, graph=True)
        profile = kernel.lane_profile(_req())
        self.assertEqual(profile.active, ("vector", "fts", "graph"))
        self.assertEqual(profile.disabled, ())
        self.assertEqual(profile.unavailable, ())
        for lane in SIMILARITY_LANES:
            self.assertIn(lane, profile.describe())

    def test_three_states_are_exhaustive_and_disjoint(self):
        """active+disabled+unavailable 必须恰好铺满车道全集——加车道忘登记会破。"""
        cases = [
            ("only-fts-wired", _kernel(fts=True), _req()),
            ("graph-not-wired", _kernel(vector=True, fts=True),
             _req(use_graph=False)),
            ("fts-off", _kernel(vector=True, graph=True),
             _req(use_fts=False)),
            ("nothing-wired", _kernel(), _req()),
            ("vector-and-graph-off",
             _kernel(vector=True, fts=True, graph=True),
             _req(use_vector=False, use_graph=False)),
        ]
        for name, kernel, request in cases:
            with self.subTest(case=name):
                profile = kernel.lane_profile(request)
                total = (len(profile.active) + len(profile.disabled)
                         + len(profile.unavailable))
                self.assertEqual(total, len(SIMILARITY_LANES))
                # 互斥：任一车道不得同时出现在两处
                seen = (list(profile.active) + list(profile.disabled)
                        + list(profile.unavailable))
                self.assertEqual(sorted(seen),
                                 sorted(SIMILARITY_LANES),
                                 f"车道必须恰在三分之一处：{profile}")

    def test_report_description_can_only_come_from_profile(self):
        """钉住「不得再有第二处车道描述」——散落的散文是本卡要切的病根。

        用 AST 查字符串常量而**不是**全文搜，并且**跳过 docstring**：
        改完后模块 docstring 里仍会解释「原来那句散文被切掉了」。
        全文搜会把说明文字误判成残留；不排除 docstring 的 AST 搜同样会。
        这里要禁的是**会被执行的那份描述**，不是提到过它的注释。
        """
        import ast

        source = (REPO_ROOT / "scripts" / "run_benchmarks.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings):
                with self.subTest(line=node.lineno):
                    self.assertNotIn(
                        "no vector lane", node.value,
                        "engine 段的车道描述必须由 lane_profile 派生，"
                        "不得再有会被执行的第二份散文")
        self.assertIn("describe()", source)

    # ── 边界 ────────────────────────────────────────────────
    def test_unavailable_is_distinct_from_disabled(self):
        """「请求要但没接线」与「请求不要」是两回事，塌成一态报告会谎称能力。"""
        kernel = _kernel(fts=True)
        profile = kernel.lane_profile(_req(use_vector=False))
        self.assertIn("vector", profile.disabled)
        self.assertNotIn("vector", profile.unavailable)
        self.assertIn("requested-but-unavailable", kernel.lane_profile(_req()).describe())

    def test_wiring_table_drives_unavailable_exactly(self):
        """LANE_WIRING 的每个属性都必须真的决定该道通不通——
        表里写错属性名 = 该道永远被误判为 unavailable 或误判为可用。"""
        for lane, attrs in LANE_WIRING.items():
            for attr in attrs:
                with self.subTest(lane=lane, attr=attr):
                    # 只接齐其余属性 → 本车道应落在 unavailable
                    kwargs = {}
                    for other, other_attrs in LANE_WIRING.items():
                        if other == lane:
                            continue
                        for a in other_attrs:
                            kwargs[a] = (lambda t: [0.0] * 4) if a == "embedder" \
                                else _StubLane()
                    kernel = QueryKernel(store=None, **kwargs)
                    profile = kernel.lane_profile(_req())
                    self.assertIn(lane, profile.unavailable,
                                  f"{lane} 缺 {attr} 时必须判为 unavailable")
                    self.assertNotIn(lane, profile.active)

    def test_vector_needs_both_collection_and_embedder(self):
        """只挂 chroma 不挂 embedder 算 vector 不可用——两样缺一这条道就点不亮。"""
        collection_only = QueryKernel(store=None, vector=_StubVector())
        self.assertIn("vector",
                      collection_only.lane_profile(_req()).unavailable)
        embedder_only = QueryKernel(store=None,
                                    embedder=lambda t: [0.0] * 4)
        self.assertIn("vector",
                      embedder_only.lane_profile(_req()).unavailable)

    def test_weights_sorted_and_comparable_across_runs(self):
        a = _kernel(vector=True, fts=True).lane_profile(_req())
        b = _kernel(vector=True, fts=True).lane_profile(_req())
        self.assertEqual(a.weights, b.weights)
        self.assertEqual([n for n, _ in a.weights], list(a.active))

    def test_anchor_reported_separately_from_similarity_lanes(self):
        """anchor 直查 canonical 不走断路器——混进相似度三分会误判断路器语义。"""
        self.assertNotIn("anchor", SIMILARITY_LANES)
        kernel = _kernel(fts=True)
        self.assertEqual(kernel.lane_profile(_req()).anchor, "closed")
        self.assertEqual(kernel.lane_profile(_req(use_anchor=False)).anchor, "off")
        self.assertIn("anchor", kernel.lane_profile(_req(use_anchor=False)).describe())

    # ── 反向（本卡核心守卫）──────────────────────────────────
    def test_degraded_lane_stays_active(self):
        """跑过 ≠ 降级 ≠ 关闭：降级三态塌成一态 = 静默失真。"""
        kernel = _kernel(vector=True, fts=True, graph=True)
        kernel.breakers["vector"].state = "open"
        profile = kernel.lane_profile(_req())
        self.assertIn("vector", profile.degraded)
        self.assertIn("vector", profile.active,
                      "降级的道确实参与了候选池，不得移出 active")
        self.assertIn("degraded: vector", profile.describe())

    def test_no_similarity_lane_never_claims_semantic_capability(self):
        profile = LaneProfile(active=(), disabled=SIMILARITY_LANES,
                              unavailable=(), degraded=(), weights=())
        self.assertEqual(profile.describe(), "no similarity lane (anchor only)")
        self.assertEqual(profile.lanes(), [])

    def test_description_has_no_double_space_or_stray_separator(self):
        """B-003 实跑抓到：拼出 "vector+fts + RRF  + anchor" 双空格。

        描述要给人读、也要给下游正则对拍，排版瑕疵会变成假差异。
        """
        for lanes in (("vector", "fts"), ("fts",), ("vector",)):
            for anchor in ("closed", "off"):
                with self.subTest(lanes=lanes, anchor=anchor):
                    text = LaneProfile(
                        active=lanes, disabled=(), unavailable=(), degraded=(),
                        weights=(), anchor=anchor).describe()
                    self.assertNotIn("  ", text)
                    self.assertFalse(
                        text.startswith(" ") or text.endswith(" "), text)
                    self.assertEqual(text, text.strip())

    def test_lane_without_weight_never_fabricates_one(self):
        """CHANNEL_WEIGHTS 里没有的车道不得被编造权重（RRF 会静默按 1.0 跑）。"""
        kernel = _kernel(fts=True)
        kernel.CHANNEL_WEIGHTS = {**kernel.CHANNEL_WEIGHTS, "fts": 0.85}
        profile = kernel.lane_profile(_req())
        self.assertEqual(dict(profile.weights), {"fts": 0.85})
        for lane, _ in profile.weights:
            self.assertIn(lane, _kernel().CHANNEL_WEIGHTS)

    # ── 反向钉 7：给了向量道却给不了 ────────────────────────
    def test_vector_requested_but_unavailable_fails_loudly_with_no_metrics(self):
        """本卡最坏结局：报告写着「vector+fts+RRF」，数字其实只经过 fts。

        所以必须非零退出，且**stdout 不得含任何指标段**——「看起来测过」
        比明着失败危险得多。

        数据集必须是**真的**：用不存在的数据集会在读到语料时先炸，
        根本走不到向量道，那样这条钉测的是「文件不存在」，不是「车道给不了」。
        """
        with _tiny_locomo() as data_path:
            env = {**os.environ, "MIMIR_V8_MODEL": "no/such-model-offline"}
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "run_benchmarks.py"),
                 "--locomo", str(data_path), "--vector",
                 "--top-k", "1,10", "--limit", "100"],
                capture_output=True, text=True, timeout=600, env=env)
        self.assertNotEqual(proc.returncode, 0, "--vector 给不了必须非零退出")
        combined = proc.stdout + proc.stderr
        self.assertNotIn("hit_rate", combined,
                         "失败时不得吐出指标段——那是一份看起来测过的报告")
        self.assertNotIn("mrr", combined)
        # 失败原因必须指名道姓，不许只抛一个 ImportError 让读者猜
        self.assertTrue(
            any(token in combined for token in ("--vector", "向量道")),
            f"报错须明写请求了向量道，实际输出：{combined[-400:]}")

    def test_vector_lane_unavailable_is_its_own_exception_type(self):
        """必须能被 CLI 单独接住。混进通用 except = 被吞 = 出假数。"""
        module = _load_runner()
        self.assertTrue(issubclass(module.BenchmarkLaneUnavailable, RuntimeError))


def _load_runner():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mimir_run_benchmarks_lane_probe",
        REPO_ROOT / "scripts" / "run_benchmarks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _tiny_locomo():
    """最小可解析的 LoCoMo 样本（1 样本 / 2 会话 / 1 题），落临时文件。"""
    import json
    import tempfile

    sample = {
        "sample_id": "t0",
        "conversation": {
            "speaker_a": "A", "speaker_b": "B",
            "session_1": [
                {"speaker": "A", "text": "I adopted a dog named Rex.",
                 "dia_id": "D1:0"},
                {"speaker": "B", "text": "Nice.", "dia_id": "D1:1"},
            ],
            "session_2": [
                {"speaker": "A", "text": "The weather is cold today.",
                 "dia_id": "D2:0"},
            ],
        },
        "qa": [{"question": "What is my dog's name?",
                "category": 1,
                "evidence": ["D1:0"]}],
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    with handle as stream:
        json.dump([sample], stream, ensure_ascii=False)
    try:
        yield handle.name
    finally:
        os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
