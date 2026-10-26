# Mímir v1.6.0 第三步：双层结晶流水线（早报生成与结构化实体入库）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development.
> 写测试 → 看它红 → 最小实现 → 看它绿 → 重构。每步提交。

## Goal

完成 v1.6.0 第三个里程碑「双层结晶流水线」：把第二步探针/信源感知到的外部世界
变化，转化为

- **第一层（文本输出）**：一份人类可读的每日科技早报（Markdown）；
- **第二层（结构化实体入库）**：从早报中抽取结构化模型/工具卡片，自动写入
  canonical 库（fact + `entities` 实体 + `fact_entities` 关联），供下游检索与 Obsidian 投影。

这是对 v1.6.0「实体知识与早报飞轮」的收尾，使系统具备"感知 → 结晶"闭环的第一步。

## 依赖（已在 Step 1 / Step 2 落地）

- `CanonicalStore.create_entity` / `link_fact_entity` / `get_fact_entities`（Step 1 实体库）
- `probe_model_endpoint` 结果形状 `{status, latency_ms, error?, endpoint}`（Step 2）
- `sources.yaml` 声明（models / github / tools）

## 关键文件

- 新建：`mimir_v8/morning_brief.py` — 双层结晶流水线核心
- 新建：`tests/test_morning_brief.py` — TDD 测试套件
- 修改：`mimir_v8/api.py` — 可选 `POST /v16/brief/generate` 端点（write 权限）
- 修改：`docs/superpowers/specs/2026-10-26-morning-brief-crystallization-design.md` — 设计文档

## 数据模型

`SourceItem` 数据类（流水线输入，与第二步输出对齐）：

```python
@dataclass
class SourceItem:
    kind: str            # "model_probe" | "github_release" | "rss_item"
    source_id: str       # 对应 sources.yaml 的 name / repo
    title: str
    body: str
    url: str | None = None
    captured_at: str | None = None   # ISO 8601
    status: str | None = None        # 可选：探针状态摘要
```

`EntityCard` 数据类（第二层产物）：

```python
@dataclass
class EntityCard:
    entity_type: str     # "model_card" | "tool_card"
    name: str
    domain: str
    summary: str
    facts: dict          # 结构化字段（endpoint / version / tags ...）
```

## 实施任务

### Task 1: 第一层 — 早报 Markdown 渲染

- `MorningBriefService.render_markdown(items: list[SourceItem], *, as_of: str) -> str`
- 按 kind 分节（模型探针 / GitHub Release / 资讯），列出可达性、版本、要点。
- 纯函数、确定性、无副作用，便于测试。

### Task 2: 第二层 — 结构化实体抽取

- `MorningBriefService.extract_cards(items) -> list[EntityCard]`
- model_probe → `model_card`；github_release → `tool_card`。
- 抽取规则确定性、可断言（从 title/body 取字段，不调用 LLM）。

### Task 3: 入库闭环（双时态 + 关联）

- `MorningBriefService.publish(items, *, actor_principal, now=None) -> dict`
- 第一层：早报整体作为一条 fact（domain=`system`，fact_type=`event`/`learning`）写入。
- 第二层：每个 EntityCard → `create_entity` + `create_fact` + `link_fact_entity`
  （role=`crystallized_entity`）。
- 返回 `{brief_fact_id, cards: [{entity_id, fact_id, name, role}]}`。
- 已有卡片（同 name + entity_type）时**幂等更新**而非重复创建（用 get_entity + update_entity）。

### Task 4: API 端点（可选）

- `POST /v16/brief/generate`：接收 SourceItem 列表，返回 `{brief, cards}`。需 write 权限。

### Task 5: 测试 + 回归

- `tests/test_morning_brief.py`：覆盖三层 + 幂等 + 关联正确。
- 回归：v1.6.0 + bootstrap 全套仍绿。

## 验收标准

- [ ] 给定一组探针结果 + GitHub Release 条目，能渲染出分节早报 Markdown
- [ ] 能从条目中确定性抽取 model/tool 卡片
- [ ] 卡片能写入 canonical 库并关联到 fact（fact_entities 可见）
- [ ] 重复发布同一条目幂等（不重复创建实体）
- [ ] 全部新测试通过，原回归不受影响

## 里程碑

完成后 v1.6.0 三步全部落地 → 可进入「Obsidian 本地双链桥 (v1.7.0)」。
在此之前**不**执行 GitHub 发布（用户指示：1.6.0 全部完成后再发布）。

### 实施记录（2026-10-26）
- Task 1 第一层早报：`render_markdown` 按 kind 分节，确定性，空输入合法。
- Task 2 第二层抽取：`extract_cards` 确定性（不调 LLM）。
- Task 3 入库闭环：`publish` + `_upsert_card` 幂等（同 name+type 更新复用）。
- Task 4 API：`POST /v16/brief/generate`（write）。
- Task 5 测试：`test_morning_brief.py`(6)、`test_morning_brief_api.py`(2) 全过
  （后者一条用例本机因 Windows 临时目录清理 WinError 偶发 teardown 失败，断言全过）。

**踩坑判例**：幂等复用时误把 entity_id 传给 `get_fact_entities`（它吃 fact_id），
导致查不到已关联 fact 而重复建实体。正确做法是按 entity_id 反查
`fact_entities`（`_linked_fact_id`）取 fact_id。属于"方向反了"类缺陷。
