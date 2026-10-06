# ③-4 判重接通（Dedup Activation）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已存在但零调用的 `check_duplicate` 接进候选写入路径——重复候选被**标记**（不阻断、不改写），审计留痕，为后续判重策略提供真实观测数据。

**Architecture（实测定形，范围比 spec 原文小）:** spec 假设"记忆只增不减、需 LLM 语义判重"，实测证伪（重复率 ≈0.5%，360 条抽样仅 2 对；`content_hash` 精确重复 0 条）。真需求 = **接通已有的 Jaccard 判重函数**（`dedup.py:check_duplicate`，实现完整含 P0-M top-K 召回优化，但全项目零调用）。本件**不做 LLM 判重**（零 LLM 成本），**不阻断写入**（观察期：只标记+留痕）。

**Tech Stack:** Python 3.11, sqlite3, unittest

**Spec:** `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md`（§三 ③-4，范围经实测修正）

## 实测依据（先量后写，2026-10-07）

| 测量 | 结果 |
|---|---|
| `content_hash` 精确重复 | 0 条 |
| 全库分层抽样 360 条 Jaccard ≥0.70 | 2 对（0.762 / 0.821） |
| 近 150 条互比 | 0 对 |
| 重复率 | **≈0.5%** |

样例（同一偏好的两次表述）：
- 「Jarvis 默认模型改 Gemini 3.7 Flash，通过 9Router」vs「…通过 9router，且计划建立一…」
- 「较新、环境好、带泳池…酒店」vs「新、环境好、带泳池…酒店」

结构性发现：`dedup.py:check_duplicate` **零调用**（只 `jaccard_similarity` 被 query.py/conflict.py 借用于他途）。

## Global Constraints

- **观察期，不阻断**：本件只**标记 + 留痕**，不拒绝候选、不改写内容。判重函数从未在生产验证过——直接阻断写入是拿生产做实验。
- **零 LLM 成本**：只用 Jaccard（本地计算）；LLM 语义判重挂起候靶子。
- **幂等不受影响**：判重结果**不得**进入 `idempotency_fingerprint`（否则重放会因判重结果不同而冲突）。
- **失败不影响写入**：判重异常必须被吞掉并留痕，**绝不阻断**候选创建（fail-open，与 fail-closed 的 lineage gate 不同——判重是观察件不是安全件）。
- **零 schema 迁移**：用既有 `uncertainty_reasons` 字段 + `audit_log`。
- TDD：先写失败测试。

---

## File Structure

- **Modify `mimir_v8/candidates.py`** — `create_candidate_in_transaction` 内、INSERT 之前加判重标记。
- **Create `tests/test_p0s_dedup_activation.py`** — 判重接通的钉。

Non-goals: LLM 判重、`dedup.py` 算法改动、schema 迁移、`query.py`/`conflict.py`、阻断式判重。

---

### Task 1: 候选写入时判重标记 + 审计留痕

**Files:**
- Modify: `mimir_v8/candidates.py`（`create_candidate_in_transaction`，INSERT 前 ~line 160）
- Test: `tests/test_p0s_dedup_activation.py`（新建）

**Interfaces:**
- Consumes: `dedup.check_duplicate(store, content, owner) -> {is_duplicate, similarity, match_type, matched_fact_id, matched_content}`
- Produces: 候选的 `uncertainty_json` 追加 `"possible_duplicate:<match_type>:<similarity>"`；`audit_log` 落一条 `dedup.candidate_check` 留痕。返回 dict 增 `"duplicate_hint"` 字段（`None` 或 `{match_type, similarity, matched_fact_id}`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p0s_dedup_activation.py
"""③-4 判重接通：候选写入时标记疑似重复（观察期，不阻断）。"""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.store import CanonicalStore


def _seed_fact(store, content, owner="mentor"):
    from mimir_v8.schema import CreateFact
    return store.create_fact(CreateFact(
        content=content, summary="摘要", owner_principal=owner,
        domain="knowledge", fact_type="reference", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)


class TestDedupActivation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        self.svc = CandidateService(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _cand(self, content, key):
        return self.svc.create_candidate(CreateCandidate(
            content=content, summary="s", proposed_owner_principal="mentor",
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=key,
        ), actor_principal="mentor")

    def test_near_duplicate_candidate_is_flagged_not_blocked(self):
        _seed_fact(self.store, "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店")
        r = self._cand("用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店", "k1")
        self.assertTrue(r.get("duplicate_hint"), "近重复候选必须被标记")
        self.assertEqual(r["duplicate_hint"]["match_type"], "merge")
        # 关键：候选仍然被创建（观察期不阻断）
        self.assertEqual(r["status"], "review_required")
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_facts WHERE candidate_id=?",
                          (r["candidate_id"],)).fetchone()[0]
        self.assertEqual(n, 1, "标记不得阻断候选创建")

    def test_unique_candidate_has_no_hint(self):
        _seed_fact(self.store, "生产库只允许经 API 写入")
        r = self._cand("今天天气不错适合出门散步", "k2")
        self.assertIsNone(r.get("duplicate_hint"), "非重复候选不得有标记")

    def test_no_facts_at_all_is_safe(self):
        r = self._cand("库里什么都没有时的第一条", "k3")
        self.assertIsNone(r.get("duplicate_hint"))

    def test_check_failure_does_not_block_candidate(self):
        """判重异常必须 fail-open——观察件绝不阻断写入。"""
        from unittest import mock
        import mimir_v8.candidates as cand_mod
        with mock.patch.object(cand_mod, "check_duplicate",
                               side_effect=RuntimeError("boom")):
            r = self._cand("判重炸了也要能写进去", "k4")
        self.assertEqual(r["status"], "review_required", "判重失败不得阻断")

    def test_audit_trail_written_on_hit(self):
        _seed_fact(self.store, "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店")
        self._cand("用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店", "k5")
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='dedup.candidate_check'").fetchone()[0]
        self.assertGreaterEqual(n, 1, "命中必须留审计痕")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_p0s_dedup_activation.py -v`
Expected: FAIL — `duplicate_hint` 键不存在（当前无判重）

- [ ] **Step 3: 实现** — `candidates.py` 顶部加 import：

```python
from .dedup import check_duplicate
```

在 `create_candidate_in_transaction` 里，`payload = {...}` 定义**之前**（~line 156，`now = utc_now()` 附近）插入：

```python
        # ③-4 判重接通（观察期）：标记疑似重复，不阻断、不改写。
        # 判重从未在生产验证过——直接阻断写入是拿生产做实验；异常必须
        # fail-open（判重是观察件，不是 lineage gate 那种安全件）。
        duplicate_hint = None
        try:
            hint = check_duplicate(self.store, validated_fact.content,
                                   validated_fact.owner_principal)
            if hint.get("is_duplicate"):
                duplicate_hint = {
                    "match_type": hint["match_type"],
                    "similarity": hint["similarity"],
                    "matched_fact_id": hint["matched_fact_id"],
                }
        except Exception:  # noqa: BLE001 - 观察件 fail-open，绝不阻断写入
            duplicate_hint = None
```

在 INSERT 之后、`return` 之前，把标记并入 `uncertainty_json` 并写审计：

```python
            if duplicate_hint:
                existing = list(command.uncertainty_reasons)
                existing.append(
                    f"possible_duplicate:{duplicate_hint['match_type']}:{duplicate_hint['similarity']}")
                connection.execute(
                    "UPDATE candidate_facts SET uncertainty_json=? WHERE candidate_id=?",
                    (canonical_json(existing), candidate_id),
                )
                connection.execute(
                    """INSERT INTO audit_log(
                        audit_id, occurred_at, actor_principal, action, resource_type,
                        resource_id, request_id, outcome, detail_json
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id(), now, actor_principal, "dedup.candidate_check",
                     "v10", candidate_id, new_id(), "success",
                     canonical_json({"candidate_id": candidate_id, **duplicate_hint})),
                )
```

返回 dict 加 `"duplicate_hint": duplicate_hint,`（`create_candidate_in_transaction` 末尾的 `return {...}`，~line 185-189）。

**replay 路径也要加**（~line 110-118 的 `if replay:` 分支）——重放时返回 `"duplicate_hint": None,`（重放不重跑判重：判重结果不入指纹，重跑也不保证同值）。**两处 return 都要有该键**，否则调用方 `r.get("duplicate_hint")` 在两路径上形态不一致。

**注意**：`payload`（幂等指纹，~line 156）**不得**包含 duplicate_hint——否则重放会因判重结果不同而冲突。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_p0s_dedup_activation.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 回归**

Run: `python -m pytest tests/ -q -k "candidate or dedup or learn" --ignore=tests/test_p0o_version_domains.py --ignore=tests/test_r8_release.py --tb=line`
Expected: 与 pristine 基线的失败集完全相同（判重是加法，不改既有行为）

- [ ] **Step 6: 提交**

```bash
git add mimir_v8/candidates.py tests/test_p0s_dedup_activation.py
git commit -m "feat(1.3.0-③-4): 判重接通（观察期）— 标记疑似重复+审计留痕，不阻断

实测靶子：重复率 ≈0.5%（360 抽样 2 对），spec 的 LLM 判重假设证伪。
本件只接通已存在但零调用的 check_duplicate（Jaccard，零 LLM 成本）：
标记进 uncertainty_json + audit_log 留痕，不阻断不改写（观察期）。
判重异常 fail-open——观察件不是安全件。幂等指纹不含判重结果。"
```

---

## Self-Review

**Spec coverage（§三 ③-4，范围已按实测修正）：**
- spec「LLM 判断重复/冲突/全新」→ **实测证伪**（重复率 0.5%，上 LLM 收益不抵成本）；降级为 Jaccard 接通。
- spec「重复→合并、冲突→保留双方」→ 本件**不做合并**（观察期只标记）；合并留待观测数据支撑后再议。
- spec「快照进编辑历史表 + 一键回滚」→ 不适用（本件不写改内容，无需回滚）。
- spec「验收：重复写入合并（库增长 0）」→ **修正**为「重复写入被标记 + 审计留痕 + 仍可创建」（观察期语义）。

**Placeholder scan:** 无 TBD。插入位置给了行号锚点，实现者读文件确认。

**Type consistency:** `check_duplicate` 返回键 `is_duplicate/similarity/match_type/matched_fact_id`（dedup.py 已定义）与消费端一致；`duplicate_hint` 在返回 dict 与测试断言两侧同名。

**风险与回退**：判重是纯加法 + fail-open，回退 = 删该段代码。观察期数据（audit_log）为后续是否上 LLM/是否阻断提供依据。

### Task 1 补充测试（replay 路径形态一致）

```python
    def test_replay_path_also_carries_hint_key(self):
        """两处 return 必须同形态——重放路径也要有 duplicate_hint 键（值 None）。"""
        r1 = self._cand("重放测试内容", "kr")
        r2 = self._cand("重放测试内容", "kr")   # 同幂等键 → replay
        self.assertTrue(r2.get("idempotent_replay"), "必须走重放路径")
        self.assertIn("duplicate_hint", r2, "重放路径也必须有该键（值 None）")
        self.assertIn("duplicate_hint", r1)
```
