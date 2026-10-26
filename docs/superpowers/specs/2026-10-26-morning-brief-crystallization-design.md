# Mímir v1.6.0 第三步：双层结晶流水线 · 设计文档

> 状态：已实现并通过测试（Step 3 完成）
> 配套计划：`../plans/2026-10-26-morning-brief-crystallization-plan.md`

## 1. 目标

把第二步探针/信源感知到的外部世界变化，转化为：

- **第一层（文本输出）**：人类可读的每日科技早报（Markdown）。
- **第二层（结构化实体入库）**：从早报中确定性抽取模型/工具卡片，自动写入
  canonical 库（fact + `entities` 实体 + `fact_entities` 关联），供下游检索与
  后续 Obsidian 投影（v1.7.0）。

这是对 v1.6.0「实体知识与早报飞轮」的收尾，形成"感知 → 结晶"闭环第一步。

## 2. 数据模型（`mimir_v8/morning_brief.py`）

### `SourceItem`（流水线输入）
`kind`（`model_probe` / `github_release` / `rss_item`）、`source_id`、`title`、
`body`、`url?`、`captured_at?`、`status?`。与 Step 2 探针结果及 `sources.yaml`
声明对齐。

### `EntityCard`（第二层产物）
`entity_type`（`model_card` / `tool_card`）、`name`、`domain`、`summary`、`facts`。

### `PublishedCard`（入库结果行）
`name` / `entity_id` / `fact_id` / `role`。

## 3. `MorningBriefService`

| 方法 | 职责 |
|------|------|
| `render_markdown(items, *, as_of)` | **第一层**：按 kind 分节渲染早报 Markdown，确定性、无副作用。空列表返回合法空早报。 |
| `extract_cards(items)` | **第二层**：model_probe→`model_card`、github_release/rss→`tool_card`，确定性（不调用 LLM）。 |
| `publish(items, *, actor_principal, now?)` | 闭环：写一条早报 fact（domain=`system`）→ 对每张卡片 `_upsert_card` → 卡片实体以 `crystallized_entity` 角色关联到早报 fact。返回 `{brief_fact_id, cards}`。 |
| `_upsert_card(card, ...)` | 幂等：同 (name, entity_type) 已存在则更新并复用已关联 fact，否则新建 entity+fact+link。 |
| `_find_entity(name, type)` / `_linked_fact_id(entity_id)` | 幂等判定辅助查询。 |

## 4. API 边界（`mimir_v8/api.py`）

`POST /v16/brief/generate`（需 `write`）— 接收 `BriefItemBody` 列表，返回
`{brief_fact_id, brief_markdown, cards}`。卡片内 `facts` 字典存于实体
`metadata_json`，`model_card` 落在 `free_model_pool` 实体域，`tool_card` 落在
`dependency_watch` 实体域（两者 fact 统一用 `system` 域，因 `free_model_pool`
非注册 fact 域）。

## 5. 与 Step 1 / Step 2 的衔接

- 复用 `create_entity` / `link_fact_entity` / `get_fact_entities`（Step 1）。
- 复用 `probe_model_endpoint` 结果形状作为 `model_probe` 类条目输入（Step 2）。
- 幂等设计避免定时任务重复运行产生重复实体（与 aiduMEI 断供欠账自愈思路一致）。

## 6. 测试覆盖

- `tests/test_morning_brief.py`：第一层渲染（含空列表）、第二层抽取（含确定性）、
  入库闭环（fact+关联）、幂等（重复发布不增实体）。
- `tests/test_morning_brief_api.py`：端点鉴权与行为。

## 7. 未覆盖 / 后续（v1.7.0）

- 早报到 Obsidian 的单向 Markdown 投影（`ObsidianProjector`）。
- 定时任务调度（每日 07:30 守护进程触发 `publish`）。
- LLM 辅助的语义抽取（本步骤采用确定性规则，保证可测与幂等）。
