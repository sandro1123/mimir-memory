# 阶段 2 回流通电实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `PublicationService` / `replace_managed_sections` / `human_content_hash` 这套已造好但零调用的机制真正通电，把 Mímir 实体写成 Obsidian 卡片，且不碰任何既有文件内容。

**Architecture:** 三条独立的防线先行（自我摄入防护 → 准入判据 → 路径白名单声明化），然后才允许第一次写库。写入只发生在**新命名空间** `90-机器收件箱/Mímir 卡片/`，不触碰任何既有文件。

**Tech Stack:** Python 3.11+、SQLite（CanonicalStore）、pytest、PyYAML。

**Spec:** `docs/superpowers/specs/2026-10-28-obsidian-knowledge-hub-design.md`（重点 §5 阶段 2、§7.1、§7.2、§7.6）

## Global Constraints

- 库按 `target_scope='shared'` 处理（§7.1 用户已裁决）。
- `60-敏感信息/` 既不可见于 `_ALLOWED_ROOTS`，也不可被读取（§7.1 + 角色表 reference_only）。
- 自我摄入防护必须在**第一次写库之前**落地并有测试专钉（§7.2）。
- 卡片正文不含 volatile frontmatter 更新，状态只走托管区块（§7.4-b）。
- 首轮只回填早报相关实体（`model_card` / `tool_card`），不做全量回填（§7.4-d）。
- 任何写库前必须有 git 快照（`GitProtector`，已在 `mimir_v8/vault_git.py`）。
- `ruff check mimir_v8/` 必须零告警（CI 硬失败）。

## Review Focus

1. **本地事实一票否决**：事实的 `egress_policy` 为 `local_only` 时是否真的没被写进会离开本机的库。
2. **自我摄入成环**：Mímir 写的卡片被再次摄入后，是否被当成新知识重新结晶。
3. **正文误伤**：更新托管区块时，托管区块之外的字节是否逐字节不变。
4. **敏感目录穿越**：`vault_path` 含 `60-敏感信息/` 或以 `../` 逃逸时是否被拒。
5. **重复发布**：同一条事实二次发布是否幂等（不产生第二张卡、不追加重复区块）。

---

## ⚠️ 计划前置：一个必须先由用户裁决的阻塞项

**实测（2026-11-01，n100 生产库 `v9/production-v9.0-20260805_214614/canonical.db`）：**

```
egress_policy:  local_only 1292 / 1292   ← 100%，无一例外
sensitivity:    internal 1291, restricted 1
visibility:     owner_only 953, shared 182, all 157
```

而 `publication.py:467` 的 `_enforce_fact_policy` 第一条现行规则是：

```python
if fact["egress_policy"] == "local_only":
    raise PublicationPolicyError("local_only facts cannot be published to Obsidian")
```

**两者相加 = 阶段 2 通电后事实准入通过数为 0。** 规格 §7.1 写的三条判据（`restricted` 拒、`local_only` 拒、`owner_only` 拒）在真实数据上会拒绝全部 1292 条。

这不是实现 bug，是**规格与数据的前提冲突**：§7.1 写裁决时假定存在一批「可外发」的事实，实际一条都没有。

**因此本计划把处置留空，等用户裁决后填入。可选路径（互斥）：**

| 路径 | 含义 | 代价 |
|---|---|---|
| **P1 保持现状** | 阶段 2 代码全部落地、准入放行 0 条，只等未来出现 `external_allowed` 事实才生效 | 通电但产出为零，无法验证「真的能写」 |
| **P2 脱敏后放行** | 对 `local_only` 事实跑 `learning.redact_text`，**脱敏后无残留则放行**，并记录 `publication_redacted=true` | 需要新的审计字段；语义上"脱敏后已无敏感内容"能否抵消 `local_only`，须用户明示 |
| **P3 库降级为私有目标** | 把库从 `shared` 改判为 `private`（即认定 Syncthing 范围仍是"本机"） | 推翻 §7.1，用户此前已明确选 shared |
| **P4 事实重标** | 批量把某批事实改判 `external_allowed` | 生产库治理动作，超出 1.7.0 范围 |

**计划默认按 P1 实现**（代码完整、闸门真实生效、放行数为 0），T2 的测试用构造事实验证放行逻辑正确；P2/P3/P4 任一被选中时，只需改 T2 的实现与测试数据，不影响其他任务。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `mimir_v8/vault_card.py`（新建） | 卡片渲染：实体 → 带 frontmatter 标记的 Markdown 文本 |
| `mimir_v8/vault_publish.py`（新建） | 发布编排：准入判据 → GitProtector 快照 → 写文件 → PublicationService 记账 |
| `mimir_v8/publication_targets.yaml`（新建） | 路径白名单声明式配置（替代 `_ALLOWED_ROOTS` 硬编码） |
| `mimir_v8/publication.py`（改） | 从 yaml 读白名单；新增三重准入判据函数 |
| `mimir_v8/collectors/vault.py`（改） | 自我摄入防护：按 frontmatter 标记跳过 Mímir 自产卡片 |
| `mimir_v8/vault_roles.yaml`（改） | 新增 `mimir-card` 角色（frontmatter 标记命中） |
| `tests/test_vault_card.py` 等（新建） | 各任务对应测试 |

---

### Task 1: 路径白名单声明化（修 §7.6 已知缺陷）

**Files:**
- Create: `mimir_v8/publication_targets.yaml`
- Modify: `mimir_v8/publication.py:23-24`（`_ALLOWED_ROOTS` / `_FORBIDDEN_ROOTS` 改为加载配置）
- Test: `tests/test_publication_targets.py`

**Interfaces:**
- Produces: `load_publication_targets(path: Path | None = None) -> dict` → `{"allowed_roots": tuple[str, ...], "forbidden_roots": tuple[str, ...], "card_root": str}`；`publication.py` 模块级 `_ALLOWED_ROOTS` / `_FORBIDDEN_ROOTS` 改为由该函数填充的 frozenset。
- Produces: `_validate_path` 行为不变（签名不变），仍拒非白名单根、仍拒 `..`。

- [ ] **Step 1: 写失败测试**

```python
def test_real_vault_roots_are_allowed():
    """白名单必须与真实库目录对得上（§7.6 缺陷 1）。"""
    from mimir_v8.publication import _validate_path

    for root in ("005-系统设计", "30-知识库", "90-机器收件箱", "10-项目"):
        assert _validate_path(f"{root}/某页.md") == f"{root}/某页.md"


def test_sensitive_root_still_forbidden():
    from mimir_v8.publication import PublicationPolicyError, _validate_path

    with pytest.raises(PublicationPolicyError):
        _validate_path("60-敏感信息/API 密钥管理.md")


def test_old_ghost_roots_are_rejected():
    """旧白名单里的 00-系统 / 30-知识 / 90-机器收件箱 是照别的库写的。"""
    from mimir_v8.publication import PublicationPolicyError, _validate_path

    for root in ("00-系统", "30-知识"):
        with pytest.raises(PublicationPolicyError):
            _validate_path(f"{root}/x.md")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_publication_targets.py -v`
Expected: FAIL（`005-系统设计` 不在现行 `_ALLOWED_ROOTS`）

- [ ] **Step 3: 写 yaml + 改 publication.py 加载**

`publication_targets.yaml` 内容：`allowed_roots` 列出真实库根目录（`004-项目档案`、`005-系统设计`、`10-项目`、`10-Projects`、`20-领域`、`30-知识库`、`40-归档`、`50-运维记录`、`70-日记`、`90-机器收件箱`），`forbidden_roots` 列出 `60-敏感信息`，`card_root` 为 `90-机器收件箱/Mímir 卡片`。`publication.py` 中 `_ALLOWED_ROOTS = frozenset(load_publication_targets()["allowed_roots"])`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_publication_targets.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑既有 publication 测试确认无回归**

Run: `python -m pytest tests/ -k "publication or document" -v`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add mimir_v8/publication_targets.yaml mimir_v8/publication.py tests/test_publication_targets.py
git commit -m "feat(vault): declare publication roots from real vault layout"
```

---

### Task 2: 三重准入判据

**Files:**
- Modify: `mimir_v8/publication.py`（新增 `is_publishable_fact`）
- Test: `tests/test_publication_eligibility.py`

**Interfaces:**
- Consumes: `mimir_v8.learning.redact_text(text) -> RedactionResult`（已有，`.redacted: bool`）
- Produces: `is_publishable_fact(fact: dict, body: str, *, target_scope: str) -> tuple[bool, str]` —— 返回 `(可否发布, 拒绝原因或 "ok")`。

**判据（§7.7 末段三重判据，全部满足才放行）：**
1. `sensitivity != 'restricted'`
2. `visibility != 'all'`（`all` = 同库其他 agent 可读）
3. `redact_text(body).redacted is False`（正文无凭据形态）
4. 按 P1 默认：`egress_policy == 'local_only'` → 拒绝，原因 `"egress:local_only"`

> 第 4 条是 P1 默认；若用户选 P2，改为「第 4 条放行但要求第 3 条同时通过，并记录 `redacted=true`」。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(
    "fact,body,expected_reason",
    [
        ({"sensitivity": "restricted", "visibility": "shared", "egress_policy": "local_only"}, "ok text", "sensitivity:restricted"),
        ({"sensitivity": "internal", "visibility": "all", "egress_policy": "local_only"}, "ok text", "visibility:all"),
        ({"sensitivity": "internal", "visibility": "shared", "egress_policy": "local_only"}, "key sk-abcdefghijklmnopqrst", "credential:redacted"),
        ({"sensitivity": "internal", "visibility": "shared", "egress_policy": "local_only"}, "ok text", "egress:local_only"),
        ({"sensitivity": "internal", "visibility": "shared", "egress_policy": "external_allowed"}, "clean body", "ok"),
    ],
)
def test_eligibility(fact, body, expected_reason):
    from mimir_v8.publication import is_publishable_fact

    ok, reason = is_publishable_fact(fact, body, target_scope="shared")
    assert reason == expected_reason
    assert ok is (expected_reason == "ok")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_publication_eligibility.py -v`
Expected: FAIL（`is_publishable_fact` 不存在 / ImportError）

- [ ] **Step 3: 实现 `is_publishable_fact`**

按上面四条判据顺序短路返回。凭据判定复用 `redact_text`，不新造正则（避免与 `SECRET_PATTERNS` 不同源）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_publication_eligibility.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/publication.py tests/test_publication_eligibility.py
git commit -m "feat(vault): four-gate publish eligibility, not label-only"
```

---

### Task 3: 卡片渲染（含自我摄入标记）

**Files:**
- Create: `mimir_v8/vault_card.py`
- Test: `tests/test_vault_card.py`

**Interfaces:**
- Produces: `render_card(*, mimir_id: str, title: str, sections: dict[str, str], tags: list[str] | None = None) -> str`
- 返回的文本首部必须是 YAML frontmatter，含 `mimir_generated: true`、`mimir_id: <id>`、`title:`；正文只含一个托管区块 `mimir-card`。

- [ ] **Step 1: 写失败测试**

```python
def test_card_carries_self_ingest_marker():
    from mimir_v8.vault_card import render_card

    text = render_card(mimir_id="ent_123", title="某模型", sections={"mimir-card": "正文"})
    assert text.startswith("---\n")
    assert "mimir_generated: true" in text
    assert "mimir_id: ent_123" in text


def test_card_managed_block_is_round_trippable():
    from mimir_v8.publication import human_content_hash
    from mimir_v8.vault_card import render_card

    a = render_card(mimir_id="ent_1", title="T", sections={"mimir-card": "第一版"})
    b = render_card(mimir_id="ent_1", title="T", sections={"mimir-card": "第二版"})
    assert human_content_hash(a) == human_content_hash(b)


def test_card_filename_is_stable_and_path_safe():
    from mimir_v8.vault_card import card_filename

    assert card_filename("ent_1", "某模型 / 带斜杠") == "ent_1.md"
```

> 第三条钉的是「卡片改名不靠标题」，因为标题里可能含 `/`、`:` 等路径/非法字符。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_vault_card.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `render_card` 与 `card_filename`**

`card_filename(mimir_id, title)` 忽略 `title`，返回 `f"{mimir_id}.md"`（保留 title 参数为将来可读名预留，但**当前实现不用它**——改文件名会让 Obsidian 双链断掉）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_vault_card.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/vault_card.py tests/test_vault_card.py
git commit -m "feat(vault): card renderer with self-ingest marker"
```

---

### Task 4: 自我摄入防护（§7.2，必须在首次写库前）

**Files:**
- Modify: `mimir_v8/collectors/vault.py`（`collect()` 中跳过带标记的卡片）
- Modify: `mimir_v8/vault_roles.yaml`（新增 `mimir-card` frontmatter 映射）
- Test: `tests/test_vault_self_ingest_guard.py`

**Interfaces:**
- Consumes: `Task 3` 的卡片 frontmatter 约定（`mimir_generated: true`）。
- Produces: `MimirCardSkipped` 计数进 `CollectResult.items_skipped`；被跳过的卡片**不产生 CollectResult**（不进学习管道）。

- [ ] **Step 1: 写失败测试**

```python
def test_mimir_card_is_not_ingested(tmp_path):
    from mimir_v8.collectors.vault import VaultCollector

    card = tmp_path / "90-机器收件箱" / "Mímir 卡片"
    card.mkdir(parents=True)
    (card / "ent_1.md").write_text("---\nmimir_generated: true\nmimir_id: ent_1\n---\n\n正文\n", encoding="utf-8")
    (tmp_path / "普通笔记.md").write_text("---\n---\n\n人类内容\n", encoding="utf-8")

    results = VaultCollector(vault_root=tmp_path).collect()
    ids = [r.source_id for r in results]
    assert "普通笔记.md" in ids
    assert not any("ent_1.md" in i for i in ids)


def test_reverse_injection_removing_guard_makes_card_ingested(tmp_path, monkeypatch):
    """反向注入：把守卫短路后，卡片必须被摄入——证明这条测试真的在守它。"""
    from mimir_v8.collectors import vault as vault_mod

    monkeypatch.setattr(vault_mod, "_is_mimir_generated", lambda *_: False)
    card = tmp_path / "card.md"
    card.write_text("---\nmimir_generated: true\nmimir_id: x\n---\n\n正文\n", encoding="utf-8")
    assert len(vault_mod.VaultCollector(vault_root=tmp_path).collect()) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_vault_self_ingest_guard.py -v`
Expected: FAIL（卡片被摄入 / `_is_mimir_generated` 不存在）

- [ ] **Step 3: 实现 `_is_mimir_generated(text) -> bool` 并在 `collect()` 中调用**

只解析 frontmatter 头部（首个 `---` 到第二个 `---`），匹配 `mimir_generated: true`（容忍行尾空白与引号）。命中则 `continue`，不构造 `CollectResult`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_vault_self_ingest_guard.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 跑既有 collector 测试确认无回归**

Run: `python -m pytest tests/ -k "vault" -v`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add mimir_v8/collectors/vault.py mimir_v8/vault_roles.yaml tests/test_vault_self_ingest_guard.py
git commit -m "feat(vault): skip Mimir-generated cards on ingestion (anti-feedback-loop)"
```

---

### Task 5: 发布编排（写库，仅新文件）

**Files:**
- Create: `mimir_v8/vault_publish.py`
- Test: `tests/test_vault_publish.py`

**Interfaces:**
- Consumes: `is_publishable_fact`（T2）、`render_card` / `card_filename`（T3）、`GitProtector`（`mimir_v8/vault_git.py`，已有）、`PublicationService.register_document` / `request_publication` / `apply_publication`（已有）。
- Produces: `publish_entity(*, vault_root: Path, store, fact: dict, entity: dict, actor_principal: str) -> dict` → `{"status": "published"|"skipped"|"idempotent", "path": str, "reason": str}`

**行为约束：**
- 目标路径恒为 `<card_root>/<mimir_id>.md`；`card_root` 取自 T1 的配置。
- 文件已存在且 `human_content_hash` 未变 → 走 `apply_publication` 更新托管区块；已存在但人类区变了 → `status="skipped"`, `reason="human_content_changed"`，**不覆盖**。
- 写前必须 `GitProtector.snapshot_before_write([target])`；目标文件不存在时快照退化为「记录当前 HEAD」。

- [ ] **Step 1: 写失败测试**

```python
def test_publish_writes_new_card_under_card_root(tmp_path, store, external_fact, entity):
    from mimir_v8.vault_publish import publish_entity

    result = publish_entity(vault_root=tmp_path, store=store, fact=external_fact, entity=entity, actor_principal=VAULT_ADAPTER_PRINCIPAL)
    assert result["status"] == "published"
    written = (tmp_path / result["path"]).read_text(encoding="utf-8")
    assert "mimir_generated: true" in written


def test_publish_is_idempotent(tmp_path, store, external_fact, entity):
    from mimir_v8.vault_publish import publish_entity

    first = publish_entity(...)
    second = publish_entity(...)
    assert second["status"] in ("idempotent", "published")
    assert first["path"] == second["path"]
    assert (tmp_path / first["path"]).read_text(encoding="utf-8").count("mimir:managed:start") == 1


def test_publish_refuses_local_only_fact(tmp_path, store, local_only_fact, entity):
    from mimir_v8.vault_publish import publish_entity

    result = publish_entity(...)
    assert result["status"] == "skipped"
    assert result["reason"] == "egress:local_only"
    assert not list(tmp_path.rglob("*.md"))


def test_publish_never_touches_human_content(tmp_path, store, external_fact, entity):
    """写第二版时，托管区块外的人类字节逐字节不变。"""
    from mimir_v8.publication import human_content_hash

    publish_entity(...)
    path = tmp_path / first["path"]
    original = path.read_text(encoding="utf-8")
    with_human_note = original + "\n\n人写的一行\n"
    path.write_text(with_human_note, encoding="utf-8")

    publish_entity(...)  # 第二版
    after = path.read_text(encoding="utf-8")
    assert "人写的一行" in after
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_vault_publish.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `publish_entity`**

顺序固定：准入 → 计算目标路径 → 快照 → 渲染 → 注册文档（首次）→ 请求发布 → 写文件 → 应用发布记录。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_vault_publish.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/vault_publish.py tests/test_vault_publish.py
git commit -m "feat(vault): publish entity cards into a new namespace, never touching human text"
```

---

### Task 6: 版本登记与 CI 接线

**Files:**
- Modify: `scripts/version_sweep.py`（登记 `publication_targets.yaml`）
- Test: `tests/test_version_sweep.py`（已有，追加一条）

**Interfaces:**
- Consumes: `scripts/version_sweep.py` 的 `REGISTRY` 列表。

- [ ] **Step 1: 写失败测试**

```python
def test_publication_targets_is_registered():
    from scripts.version_sweep import REGISTRY

    assert any("publication_targets" in entry["path"] for entry in REGISTRY)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_version_sweep.py -v`
Expected: FAIL

- [ ] **Step 3: 在 `REGISTRY` 追加条目**（不加版本号则 `version_sweep` 报 STALE）

- [ ] **Step 4: 跑测试与扫描器**

Run: `python -m pytest tests/test_version_sweep.py -v && python scripts/version_sweep.py`
Expected: PASS + 全 OK

- [ ] **Step 5: Commit**

```bash
git add scripts/version_sweep.py tests/test_version_sweep.py
git commit -m "chore: register publication targets in version sweep"
```

---

## Self-Review

**1. Spec coverage:** §5 阶段 2 的三项产出（Publisher 通电、PublicationService 接入、新命名空间）→ T5；§7.2 自我摄入 → T4；§7.6 四项缺陷 → T1（白名单）、T3（frontmatter 计入人类区：卡片 is 全托管，故 `human_content_hash` 稳定，已由 T3 第二条测试钉住）、T5（`entity_card` 类型走 `DOCUMENT_TYPES` 扩枚举的替代方案 = 复用既有 5 值之一，**需确认是否可接受**）；§7.4-d 只回填早报实体 → 未在本计划内（T5 只提供能力，批量回填留待阶段 3/4）。

**2. Gap identified:** `DOCUMENT_TYPES` 无实体卡片类型（§7.6 第三行）。本计划的 T5 未解——需在实现时决定：扩枚举（schema 迁移 + `version_sweep`）还是复用 `"overview"`。**这是本计划的已知空白，执行前须定。**

**3. Type consistency:** `is_publishable_fact` 返回 `tuple[bool, str]`，T5 消费；`render_card` 关键字参数在 T3 定义、T5 调用，签名一致；`card_filename(mimir_id, title)` 两处一致。

**4. Review Focus 对应:** 1→T2/T5、2→T4、3→T5 第四条、4→T1、5→T5 第二条。

**5. Proportion:** 计划长度约为规格的 1/3，每个代码步骤给签名不给实现体，符合要求。
