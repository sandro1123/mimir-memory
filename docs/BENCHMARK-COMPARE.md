# 对标对照表（1.7→2.0 路线图 §4 产出）

> 文档日期：2026-11-13
> 性质：§4 要求的一次性对照基线。只借能回答四问的机制。
> 四问：① 自动触发是什么 ② 遗忘靠什么 ③ 热度怎么算 ④ 怎么防回声闭环

## 一、结论先行（给新 Agent 的上下文）

| 本结论 | Mímir 现状（实测，2026-11-13） | 对标出处 |
|---|---|---|
| `search_feedback` 只写不读 `_decay_factor`——真实缺口 | `evolve.py:74` INSERT、`evolve.py:140` 按 useful/useless 加权 confidence；`query.py:1221 _decay_factor` 静态 `DECAY_HALF_LIFE`，从未读 feedback | §5.3-3，实测 grep 证实（`query.py` 无 search_feedback 引用） |
| 热度无 `fusionCount`/`recallCount` 双轨 | 无两表（`search_feedback` schema 见下），无 per-fact 计数 | dsh `halfLifeOf()+spacingQuality()+fusionCount/recallCount 双衰减` |
| 无自动 STM 层（长时暂存层是 `candidate_facts`，非 STM） | `ingest_conversation` 在，`session_episode` 无（#34 未做） | claude-mem SessionStart/SessionEnd + Letta 分层上下文 |

`search_feedback` schema（`store.py:624`，migration v14→15）：
```sql
feedback_id TEXT PK, query_text TEXT, fact_id TEXT,
signal IN (useful/useless/correction), user_principal TEXT, created_at TEXT
```
生产实测（`C:\Users\sandr\.hermes\mimir\data\canonical.db`）：facts=0 / memory_events=0（空库，生产数据在 n100，不在此）。迁移回填成本待 n100 实测。

---

## 二、四问对照表（每行回答四问）

### 2.1 热度/衰减机制（Mímir §5.3 的核心借鉴）

| 机制 | 对方怎么算 | 对方触发 | Mímir 的对照动作（§5.3） |
|---|---|---|---|
| **dsh `halfLifeOf()`** | 按**活跃日**非 importance：`base * f(log2(1+strength))`，strength 來自重复命中 | 每次 `attend()` | `_decay_factor` 改为活跃日半衰期（f(active_days) 后接 `DECAY_HALF_LIFE`） |
| **dsh `spacingQuality`** | 间隔越长提升越大：`q = min(1, 0.15+0.85·log2(1+ratio))` | 两次命中间隔 | `ratio = gap_days / base_half_life` |
| **dsh 双轨计数** | `fusionCount`（内容重复，累计）vs `recallCount`（`mem.attend(ids)` 确认使用，非搜索次数），两者都衰减 | 内容重复 + 确认使用两条独立记账 | Mímir 补两计数列；`search_feedback` 的 read 接 `_decay_factor`（useful→+热，correction→-热） |
| **Mímir 现行** | 静态 `DECAY_HALF_LIFE` 期望：`evolve.py` 按 useful>useless Δconfidence 0.05（`CONFIDENCE_DELTA`，`MIN_SIGNALS_PER_FACT` 门槛） | 定时 `evolve` | 保留 opinion 通道，新增 `_decay_factor` 热度通道，两通道不互斥（意见是置信，热度是排序） |

**没借的**：dsh `fusionPrefilter 0.82`（向量预筛）——Mímir 已有 FTS trigram + RRF，无需新增阶段；`buildBlock` 升序保前缀缓存是 LLM 上下文优化，不属记忆库层。

**先量后写戒律**：dsh 间隔加权“跨越 1 天重复 0.35 / 14 天 0.70”的长期召回增益，在 Mímir 侧尚无对等实测；1.8 热度上线前须在 `docs/BENCHMARK-PROTOCOL.md` 下半日回放对比（口径随数字同行：`engine` 字段、`mimir_version`/`schema_version`/`run_at` 三件齐全）。

### 2.2 自动捕获触发（Mímir §5.2）

| 档 | harness | 对方协议 | Mímir 捕获协议 |
|---|---|---|---|
| **A 钩子型** | claude-mem（SessionStart/SessionEnd/PostToolUse） | 后台静默摄入 | SessionStart 拉 `/v15/bootstrap`；SessionEnd POST `ingest_conversation`；工具调用只记 trajectory 摘要 |
| **B MCP 型** | Cursor / Zed / Windsurf（无 hooks）| MCP 工具调用本身即触发 | `mimir_*` 工具调用即记录，降级为显式档 |
| **C 显式型** | DeepSeek 网页/App/Code、任意 HTTP 端点 | 无 hooks/MCP | `POST /v8/ingestion/conversations` + token 经 `POST /v8/principals/register`（#30 已落地） |

**STM→LTM 纪律**：STM 在会话结束时自动**结晶为 candidate_facts**（走 Tier-0/1/2 判重 → 评审 → 入库），**不直写 facts**。自动获取 ≠ 自动入库（AGG 全自动，UPDATE/DELETE 永不自动，沿 1.5.0 判词；见 §5.1）。

### 2.3 dsh 竞争生态（9 个插件）

| 插件 | 自动触发 | 遗忘 | 热度 | 防回声 |
|---|---|---|---|---|
| `dsh-memory` | SessionStart 注入 | TTL + 手动 purge | 活跃日半衰期 | 无 feedback 无加热 |
| `dsh-auto-memory` | SessionEnd 自动摄入 | 摘要合并 | 同上 | 无 ledger 不删 |
| `dsh-mnemon` | 冲突检测后结晶 | supersede 链 | spacingQuality | 不覆盖历史 |
| `dsh-layered-memory`（L0–L3） | 分层门限 | 逐层下沉 | `halfLifeOf` | 置信进层 |
| `dsh-tdai-memory` | 跨会话连续 | 关键词+语义双衰减 | fusion+recall 双轨 | `attend` 才计 |
| `dsh-memory-evolve` | 评估闭环 | 低分踢层 | 进化回写分数 | 评估衰减 |
| `dsh-self-improved` | 任务完成自评 | 失败路径不记 | 成功路径加热 | 不自证 |
| `dsh-hybrid-memory` | 混合检索触发 | 视图层过期 | 多信号加权 | 画像只读 |
| `stratagate-dsh` | 配置驱动规则 | 规则驱动 | 配置即权重 | 配置即约束 |

**Mímir 的映射**：自动触发挑 2–3 个实测数值反推默认值（`MIN_SIGNALS_PER_FACT`、`CONFIDENCE_DELTA` 现有值是经验值，须与对标实测对过）；没对等实测的不借。

### 2.4 遗忘机制

| 系统 | 遗忘方式 | 触发 | 账本要求 |
|---|---|---|---|
| Mímir 现行 | `_decay_factor` 降权 + optional tombstone | decay-scan 定时 | `tombstone_snapshots` 等价账本（须补反向注入钉：账本写失败→不删） |
| dsh | LTM TTL + 内容合并 | 覆盖率门限 | 有 ledger 才删 |
| MemOS | 置信降权 + 长期未用下沉 | 时间 | 可回放 |
| Letta/MemGPT | cold block 交换 + compaction job | 上下文压力 | 分层块可重取 |

**F1-F5 映射**（§7.5）：F2 热度双轨、F5 过期即遗忘（ledger 先行）、F1 STM/LTM（自动捕获）、F3 Adapter Contract（接入）、F4 持续跑分（`evolve_report` + GOLDEN_FLOORS 门禁）。

### 2.5 防回声闭环（第一 anti-pattern）

| 防法 | 对方实现 | Mímir 对应 |
|---|---|---|
| 只认 `attend` 不认 search | dsh `recallCount`（`mem.attend(ids)`） | `search_feedback` 里只有 useful/correction 进热度；单纯搜索不加热 |
| 画像只读 | `dsh-hybrid` / Memobase | `core_memory_items` 视图层（只读投影，见 §5.1） |
| 有 ledger 才删 | dsh “no ledger → no delete” | `tombstone_snapshots` 先写后删，无账本不删 |
| 无模型不摘要 | dsh “no model → no summary” | 无 LLM 时只走 Tier-0 content_hash 合并（§5.4） |
| 不自证 | `dsh-self-improved` | 评估回写分数衰减，不抬升自身 |

### 2.6 双池/画像（MemOS + Honcho 对标）

| 对方 | 做什么 | Mímir §12.5 的映射 |
|---|---|---|
| MemOS | Memory 池 vs Knowledge 池 + MVCC 时态索引 | 双池查询路由（`query.py` 确定性规则：`fact_type==reference && entity 关联` → Knowledge 通道，结果标注 `pool`，预算分开） |
| Honcho | topic/sub_topic 只读投影 + 偏好自动更新 | `mimir_entity_get` 投影 + `runbook_get` 的 `battle_tested`（成功≥3 次标注，衔接 `autoskill.py` L3 判据） |
| Memobase | 结构化 topic/sub_topic 蒸馏 | 1.8 crystallize 的升级路径对照 |

---

## 三、遗忘/衰减维度速记（给新开 Agent）

- `_decay_factor` 唯一出口：`query.py:1221`，输入 `decay_tier/valid_to/updated_at`，输出 `[0..1]`。
- `DECAY_HALF_LIFE`（`schema.py:24`）是半衰期字典，`L0_never` 为永不衰减。
- `search_feedback` 落点：`store.py:624`（schema）、`evolve.py:22`（migration DDL 声明）、`evolve.py:74`（写入）、`evolve.py:140`（读来加 opinion）。
- 新热度机制**不能改意见通道语义**（意见=置信，热度=排序）。

## 四、没借的（刻意不做，§7.5）

- 情绪识别、意图推断、跨用户画像合并（“显得多”的功能，违背非必要不加）。
- 向量预筛（`fusionPrefilter 0.82`）——已有 FTS+RRF。
- JSON 文件锁 / 外部向量库（SQLite WAL + chroma 已是架构默认）。
- 多跳图推理引擎（`entity_relations` 二跳以内 SQL 就够，留 2.0 后）。
