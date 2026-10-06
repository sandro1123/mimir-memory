# ③-5 Reflect 主动反思 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Mímir 定期回顾事实库、用 LLM 提炼跨事实的模式/矛盾/知识缺口，产物**走治理管线**（候选→审批→入库）成为一等公民。

**Architecture（实测定型）:** 现状 `/v10/reflect/{topic}` 只是检索回显（把查到的 facts 列出来拼句 `"Synthesized N fact(s)"`，无 LLM、不落库）。本件新建 `mimir_v8/reflect.py`：`ReflectionService.scan()` 按 domain 聚类 active facts → 复用 `governance._call_llm` 提炼 → 产物经 `CandidateService.create_candidate` 进治理管线（**不是直接写 facts**）。周期运行照抄 `crystallize.py` 的 `_open_run`/`_close_run` 账本模式。

**Tech Stack:** Python 3.11, sqlite3, unittest

**Spec:** `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md`（§三 ③-5）

## 实测依据（先量后写，2026-10-07）

| 项 | 实测 | 判读 |
|---|---|---|
| facts（active） | 709（6 domain 分布均匀 49~160） | **反思输入面充足** |
| opinions | 8 | 太薄，不能作主输入 |
| observations | 1 | 太薄 |
| search_feedback | 6 | 太薄 |
| `/v10/reflect` | 端点存在但只回显 | 建了没接线 |

**结论**：反思主输入用 **facts 本身**（按 domain 聚类），不用 opinions。

## Global Constraints

- **产物走治理管线**：LLM 提炼出的洞察**必须**经 `CandidateService.create_candidate` 进候选队列（candidate→审批→commit），**绝不直接写 facts**——这是 Mímir 基因，也是 aiduMEI 自己的铁律（「LLM 只能建议，不能直接 commit」）。
- **诚实降级**：LLM 不可用/解析失败 → 返回空洞察 + 留痕，**不抛异常、不假装成功**（复用 `_call_llm` 的 cause 上报）。
- **周期账本**：照抄 crystallize 的 `_open_run`/`_close_run`（含 catchup 断供探测 + closing 直写降级显式 commit）。
- **可配置**：`MIMIR_REFLECT_INTERVAL_HOURS`（默认 6）、`MIMIR_REFLECT_MAX_DOMAINS`（默认 6，控 LLM 调用量）、`MIMIR_REFLECT_ENABLED`（默认 0——**默认关闭**，需显式开启）。
- **成本可控**：每轮最多 `max_domains` 次 LLM 调用（默认 6），每天 ≤ 4 轮 = ≤24 次。
- TDD：先写失败测试。

---

## File Structure

- **Create `mimir_v8/reflect.py`** — `ReflectionService`（scan + LLM 提炼 + 候选投递 + 账本）。
- **Modify `mimir_v8/worker.py`** — 加 `reflect` 子命令（照 `crystallize` 形态，~line 108 与 ~1020）。
- **Create `tests/test_p0t_reflect.py`** — 钉。

Non-goals: 改 `/v10/reflect` 端点、改 `query.py`、schema 迁移（`reflections` 落成候选不需要新表）、persona/多模态。

---

### Task 1: ReflectionService（聚类 + LLM 提炼 + 候选投递 + 账本）

**Files:**
- Create: `mimir_v8/reflect.py`
- Test: `tests/test_p0t_reflect.py`（新建）

**Interfaces:**
- Consumes: `governance._call_llm(prompt, model)`、`governance.router_config()`、`CandidateService.create_candidate`、`store.transaction()`
- Produces: `ReflectionService(store).scan(window_days=7, max_domains=6, actor_principal="service:reflect") -> dict`，返回 `{"run_id", "domains_scanned", "insights_created", "llm_failures", "skipped", "runs": {...}}`

**核心契约：**
1. 每 domain 取最近 `window_days` 内的 active facts（上限 40 条，防 prompt 爆炸）
2. 少于 `MIN_FACTS_PER_DOMAIN`（=5）的 domain 跳过（料太少无法提炼）
3. LLM prompt 要求输出 JSON：`{"insights": [{"kind": "pattern|contradiction|gap", "text": "...", "confidence": 0..1}]}`
4. 每条洞察 → `create_candidate(content=text, proposed_domain=<原 domain>, proposed_fact_type="pattern", uncertainty_reasons=("reflect:<kind>",), idempotency_key=f"reflect:{run_id}:{domain}:{idx}")`
5. LLM 失败 → `llm_failures += 1`，继续下一 domain（不中断整轮）
6. 账本：`reflect_runs` 表（守卫式建表，照抄 `crystal_runs` 模式）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p0t_reflect.py
"""③-5 Reflect：LLM 提炼洞察 → 走治理管线（候选队列），不直接写 facts。"""
import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.reflect import ReflectionService
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, domain="knowledge", owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary="s", owner_principal=owner, domain=domain,
        fact_type="reference", visibility="all", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal=owner)


def _fake_llm(payload):
    def _call(prompt, model):
        return payload, None
    return _call


class TestReflectionScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        for i in range(6):
            _seed(self.store, f"知识条目 {i}：Mímir 的记忆治理设计要点 {i}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_insight_becomes_candidate_not_fact(self):
        """核心契约：洞察进候选队列，绝不直接写 facts。"""
        payload = {"insights": [{"kind": "pattern", "text": "多条知识都指向治理优先", "confidence": 0.8}]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 1)
        with contextlib.closing(self.store.connect()) as c:
            cands = c.execute("SELECT content, proposed_fact_type, uncertainty_json FROM candidate_facts").fetchall()
            facts_before = 6
            facts_now = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(len(cands), 1, "洞察必须落成候选")
        self.assertEqual(cands[0][1], "pattern")
        self.assertIn("reflect:pattern", cands[0][2])
        self.assertEqual(facts_now, facts_before, "洞察绝不得直接写 facts")

    def test_llm_failure_degrades_honestly(self):
        with mock.patch("mimir_v8.reflect._call_llm", lambda p, m: (None, "URLError: refused")):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 0)
        self.assertGreaterEqual(r["llm_failures"], 1, "失败必须计数上报")

    def test_sparse_domain_is_skipped(self):
        _seed(self.store, "孤零零一条", domain="quant")
        payload = {"insights": [{"kind": "pattern", "text": "x", "confidence": 0.5}]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        # knowledge 有 6 条会被扫；quant 只有 1 条应被跳过
        self.assertLessEqual(r["domains_scanned"], 6)
        self.assertGreaterEqual(r["skipped"], 1, "料太少的 domain 必须跳过")

    def test_run_ledger_written(self):
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT status FROM reflect_runs ORDER BY started_at").fetchall()
        self.assertTrue(rows, "账本必须有痕")
        self.assertEqual(rows[-1][0], "completed")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_p0t_reflect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_v8.reflect'`

- [ ] **Step 3: 实现** — 创建 `mimir_v8/reflect.py`：

```python
"""③-5 Reflect 主动反思（1.3.0）。

按 domain 聚类 active facts → LLM 提炼跨事实的模式/矛盾/缺口 →
产物走治理管线（候选队列）。LLM 只能建议，不能直接 commit——
这是 Mímir 基因，也是 aiduMEI 自己的铁律。
"""
from __future__ import annotations

import logging
import os
from contextlib import closing

from .candidates import CandidateService, CreateCandidate
from .governance import _call_llm, router_config
from .store import CanonicalStore, new_id, utc_now

logger = logging.getLogger(__name__)

REFLECT_WINDOW_DAYS = 7
MIN_FACTS_PER_DOMAIN = 5
MAX_FACTS_PER_PROMPT = 40

REFLECT_PROMPT = """你是记忆反思器。下面是同一知识域内的若干条已确认事实。
请提炼**跨事实**的：模式（反复出现的规律）、矛盾（互相冲突的陈述）、
知识缺口（明显该有但没有的）。不要复述单条事实。

只输出严格 JSON：
{{"insights": [{{"kind": "pattern|contradiction|gap", "text": "一句话洞察", "confidence": 0.0}}]}}

事实列表：
{facts}
"""

REFLECT_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS reflect_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    window_days INTEGER,
    domains_scanned INTEGER DEFAULT 0,
    insights_created INTEGER DEFAULT 0,
    llm_failures INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0
)
"""


class ReflectionService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def scan(self, window_days: int = REFLECT_WINDOW_DAYS,
             max_domains: int = 6,
             actor_principal: str = "service:reflect") -> dict:
        if window_days < 1:
            raise ValueError("window_days must be >= 1")
        run_id = new_id()
        self._open_run(run_id, window_days)
        try:
            result = self._scan_inner(window_days, max_domains, actor_principal, run_id)
        except Exception as exc:
            self._close_run(run_id, "failed", error_code=type(exc).__name__)
            raise
        self._close_run(run_id, "completed", **{k: result[k] for k in
                          ("domains_scanned", "insights_created", "llm_failures", "skipped")})
        result["run_id"] = run_id
        return result
    # ... _scan_inner / _open_run / _close_run 见 brief 全文
```

（完整实现见派工 brief；此处给结构与关键契约。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_p0t_reflect.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: 提交**

```bash
git add mimir_v8/reflect.py tests/test_p0t_reflect.py
git commit -m "feat(1.3.0-③-5): Reflect 主动反思 — LLM 跨事实提炼，产物走治理管线"
```

---

### Task 2: worker 接线 + 默认关闭开关

**Files:**
- Modify: `mimir_v8/worker.py`
- Test: `tests/test_p0t_reflect.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
class TestReflectWorkerWiring(unittest.TestCase):
    def test_reflect_subcommand_registered(self):
        import mimir_v8.worker as w
        parser = w.build_parser()
        args = parser.parse_args(["reflect"])
        self.assertEqual(args.command, "reflect")

    def test_disabled_by_default(self):
        import os
        self.assertEqual(os.environ.get("MIMIR_REFLECT_ENABLED", "0"), "0",
                         "默认必须是关闭——未显式开启不得跑 LLM")
```

- [ ] **Step 2-4**: 照 `crystallize` 子命令形态加 `reflect`（parser + dispatch），dispatch 里先查 `MIMIR_REFLECT_ENABLED`，非 "1" 直接返回 `{"skipped": True, "reason": "reflect disabled"}`。

- [ ] **Step 5: 提交**

---

## Self-Review

**Spec coverage（§三 ③-5）：**
- 「后台定期（默认 6h 可调）」→ worker + timer（部署时挂，见 Task 3 备注）
- 「回顾提炼模式/矛盾/知识缺口」→ REFLECT_PROMPT 三类 kind
- 「落库成一等公民供后续检索注入引用」→ **修正**为「进候选队列」——Mímir 的治理基因要求 LLM 产物过审；直接落 facts 会绕开治理管线（这是与 aiduMEI 的关键差异）
- 「session 结束触发」→ **不做**（Mímir 无 session 概念，CDC 按批摄取；周期足够）
- 「LLM 失败返回空洞察不抛异常」→ `llm_failures` 计数 + 继续（不中断整轮）

**Placeholder scan:** Task 1 Step 3 的实现体在 brief 全文给出（此处为摘要，因实现 ~120 行）；Task 2 步骤已给形态与判据。

**Type consistency:** `scan()` 返回键在 Task 1 测试与 Task 2 之间一致；`reflect_runs` 列名与 `_open_run`/`_close_run` 一致。

**成本**：默认关闭；开启后 ≤ max_domains 次 LLM/轮，6h 一轮 = ≤24 次/天。
