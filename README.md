# Mímir — 面向多智能体与人类的统一智能中枢与长久知识库

> **The Local-First Unified Brain for All Your AI Agents & Obsidian.**  
> 一份共享记忆，贯通多个智能体；一个长久知识库，无缝映射至本地 Obsidian。  
> 融合事件溯源、实体图谱、自主外部感知与不可篡改审计，让 AI 协作不再失忆，让个人知识持续自愈。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-21-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README_en.md) · [简体中文](README.md)

---

## 为什么选 Mímir · Why Mímir

市面上的记忆系统大多「**来者不拒**」：Agent 说记就记，没有审批、没有账本、没有撤销。
Mímir 从第一天起就是反着设计的——**每一条记忆都要过审、有账、可悔**：

| 硬核特性 | 说明 |
|---|---|
| **双池一桥架构 (2.0)** | 主观会话偏好（Memory 池）与客观技术实体（Knowledge 池）物理分离，通过事实-实体桥精准多跳召回 |
| **治理管线与物理闸** | 新记忆先进候选队列，带 Tier-0/1/2 三档判重；`block_strong` 物理拦截完全重复；带 72h 待审老化告警 |
| **事件溯源与防篡改** | 52 张核心表全量溯源 + `previous_version_hash` 密码学哈希链，从写入起全程可审计 |
| **细粒度 ACL 与联邦** | 跨 Agent 共享阅读（技术域打破孤岛、私密域物理防穿透）；支持多节点 Fernet 加密联邦同步 |
| **诚实遥测与预算控制** | 查询带 `recall_verdict ∈ {found, not_found, degraded}`；超 Token 显式暴露丢弃项，严禁虚假成功 stub |
| **Obsidian 本地双链桥** | 实体事实自动投影为标准 Markdown + YAML Frontmatter + `[[双链]]`；带本地 Git 隐形快照安全气囊 |

> **当前生产版本：`1.5.0 — Federation & Shared Memory（联邦与共享）`**  
> 打破 84% 孤岛，技术域事实默认跨 Agent 共享；Principal 别名继承梯平滑退役冗余 profile；支持 `X-Mimir-Agent` 动态 Agent 零配置自举与 `mimir_bootstrap` 工具。  
> 完整 2.0 规格设计详见：[`docs/superpowers/specs/2026-10-26-mimir-2.0-unified-brain-design.md`](docs/superpowers/specs/2026-10-26-mimir-2.0-unified-brain-design.md)。

---

## 快速开始（60 秒版）· Quickstart

### 1. 启动 Mímir 核心服务 (Local-First)

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory

# 一键启动（自动安装依赖、拉取本地嵌入模型并初始化服务）
./bootstrap.sh
```

服务默认在 `http://127.0.0.1:8456` 启动。访问健康检查确认状态：
```bash
curl http://127.0.0.1:8456/health
# 返回: {"status":"ok","service":"mimir-api","version":"1.5.0","schema_version":21}
```

### 2. 挂载到你的 AI 智能体

#### 选项 A：Claude Code / Cursor / Windsurf (通过标准 MCP)
在 `~/.claude/settings.json` 或 Cursor MCP 配置中添加一行：
```bash
claude mcp add mimir -- mimir-mcp
```
Agent 瞬间获得 `mimir_query`, `mimir_write`, `mimir_bootstrap` 等全套原生工具。

#### 选项 B：Hermes Agent 接入
在 `~/.hermes/config.yaml` 中启用全局 Memory Provider：
```yaml
memory:
  provider: mimir_memory_provider
```
主 Agent（如 `Heimdallr-EX`）在每一轮对话前自动预取上下文，无需额外手动配置。

#### 选项 C：任意自定义 Agent (通过 HTTP Header 零配置自举)
新 Agent 发起请求时只需携带身份头：
```bash
curl -H "Authorization: Bearer <TOKEN>" \
     -H "X-Mimir-Agent: my-custom-agent" \
     http://127.0.0.1:8456/v15/bootstrap
```
Mímir 自动收录该 Agent，并下发当前系统的底线铁律（L0）、用户偏好（L1）与知识领域大纲，消除冷启动失忆症。

---

## 核心架构：双池一桥 (Dual-Pool & Bridge Architecture)

Mímir 2.0 将“面向机器的上下文记忆”与“面向人类的结构化知识”在物理上清晰解耦，通过事实-实体桥实现统一多跳检索：

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                 多 Agent 接入层 (Universal Front)         │
                  │   Claude Code │ Hermes (Heimdallr-EX) │ Codex │ Cursor │ REST │
                  └────────────────────────────┬────────────────────────────┘
                                               │ (统一 MCP / Zero-Config SDK)
                                               ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │              Mímir Core 协调引擎 (The Arbiter)            │
                  │   • 语义路由  • 判重与冷启动门  • 诚实预算管控  • 访问控制   │
                  └──────────────┬───────────────────────────┬──────────────┘
                                 │                           │
          ┌──────────────────────▼──────┐             ┌──────▼──────────────────────┐
          │     A. 动态记忆池 (Memory)   │             │     B. 结构化知识池 (Knowledge) │
          │  (主观、情境化、以“我”为中心)  │             │   (客观、系统化、以“事实”为中心) │
          ├─────────────────────────────┤             ├─────────────────────────────┤
          │ • 会话日志与意图 (Sources)   │             │ • 实体图谱 (Entities & Rel) │
          │ • 个人偏好 / 底线铁律 (L0-L1)│  桥接索引   │ • 技术方案与踩坑手册 (L2-L3) │
          │ • 任务执行轨迹与反思         │◄───────────►│ • 外部监控与模型资产池      │
          │ • 事件时序流水 (Event Sourcing)│(fact_entity)│ • 自动衰减与版本演进 (L4-L5) │
          └──────────────┬──────────────┘             └──────────────┬──────────────┘
                         │                                           │
                         │                                           ▼
                         │                            ┌─────────────────────────────┐
                         │                            │    人类交互界面 (Human Face) │
                         │                            │   【Obsidian 本地双链知识库】 │
                         │                            ├─────────────────────────────┤
                         │                            │ • 每日早报与盯梢动态         │
                         │                            │ • 结构化技术卡片 (Wikilinks)│
                         └───────────────────────────►│ • 个人深度笔记 (人类思考保护)│
                                                      └─────────────────────────────┘
```

---

## 四大核心支柱

### 1. 联邦记忆与跨 Agent 共享 · 告别孤岛
- **打破 84% 孤岛**：技术域（`infrastructure`、`knowledge`、`tech_support`）的参考事实默认对同机已认证 Agent 放开共享读取；`personal` 个人私密域与 `restricted` 高敏事实保持严格 owner 物理隔离。
- **Principal 别名继承梯**：主 Agent `Heimdallr-EX`（`heimdallr`）零数据库刷库无缝继承 469 条 `mentor` 历史记忆与读写权限。
- **跨节点 CRDT 联邦**：多个独立 Mímir 实例通过加密事件账本实现跨机器知识同步（详见 [docs/FEDERATION.md](docs/FEDERATION.md)）。

### 2. 结构化实体与自主感知飞轮 · 从记忆到知识
- **早报感知流水线**：后台守护进程定期拉取 GitHub Releases、技术 Feeds 与免费模型可用性探针，生成今日科技早报。
- **双层结晶流水线**：不仅产出人类快报，更自动提炼出客观可执行的 `ToolCard` 与 `Runbook` 实体卡片入库。
- **断供欠账自愈重放**：上游 API 发生 429 或故障时自动转入规则兜底并标记欠账，待网络恢复后自动重放补算，坚决不丢弃技术资讯。

### 3. 本地 Obsidian 双链桥 · 人机共生
- **第五投影器 (`ObsidianProjector`)**：底层知识变更时，自动将实体渲染为标准的 Obsidian Markdown（带 YAML Frontmatter、Tags 与 `[[双向链接]]`）。
- **人类思考绝对保护锁**：Obsidian `Personal/` 目录设置单向物理防改写锁，AI 严禁覆盖人类手写思考笔记。
- **Git 隐形快照安全气囊**：机器在执行卡片重构或合并前，底层自动提交本地 Git 快照，人类随时可一键撤销。

### 4. 硬核防腐治理与诚实遥测 · 架构深耕
- **三档判重防御**：Tier-0（O(1) 哈希精确合并）+ Tier-1（Jaccard 模糊匹配 `is_fuzzy_ready` 门控）+ Tier-2（人工审阅）；开启 `block_strong` 物理硬拦截完全重复事实。
- **待审老化守护**：`ReviewQueue` 实时纳管 `human_review`，带 72 小时 `stale` 老化告警，彻底杜绝死信箱积压。
- **诚实预算控制**：恪守 1.4.0 预算协议，超出 Token 上限时显式返回 `budget_exceeded` 与丢弃清单，严禁使用虚假 stub 冒充完整内容。
- **三段式渐进披露**：支持服务端 HMAC 游标签名，按“索引 (~50 Token) → 时间线 → 展开全文 (~500 Token)”逐步下发，杜绝上下文浪费。

---

## 能力矩阵

| 维度 | 能力特性 | 状态 |
|---|---|---|
| **核心架构** | 双池一桥架构 (Memory 池 vs Knowledge 池物理解耦) | 🎯 2.0 规格就绪 |
| | 全量事件溯源 (52 张核心表，不可篡改触发器) | ✅ 生产实证 |
| | 密码学防篡改哈希链 (`previous_version_hash`) | ✅ 生产实证 |
| **多 Agent 联邦** | 跨 Agent 共享阅读 (技术域打破 84% 孤岛，私有域物理防穿透) | ✅ 生产实证 (1.5.0) |
| | Principal 别名继承梯 (平滑退役 mentor，Heimdallr-EX 无感接管) | ✅ 生产实证 (1.5.0) |
| | 动态 Agent 零配置自举 (`X-Mimir-Agent` + `/v15/bootstrap`) | ✅ 生产实证 (1.5.0) |
| | 跨节点 CRDT 联邦 (Lamport 时钟 + Fernet 加密信封) | ✅ 生产实证 |
| **治理与防腐** | 三档判重模型 (Tier-0 精确 / Tier-1 模糊 / Tier-2 审阅) | ✅ 生产实证 |
| | 物理拦截闸门 (`MIMIR_DEDUP_GATE=block_strong`) | ✅ 生产实证 |
| | 待审队列治理与 72h 老化告警 (`backlog_aging`) | ✅ 生产实证 |
| | 双向会话上下文抽取 (保留 Assistant 方案与实体专名) | ✅ 生产实证 (1.4.1) |
| **检索与遥测** | 六阶段 RECALL 召回漏斗与诚实判语 (`recall_verdict`) | ✅ 生产实证 |
| | 诚实预算管控 (`budget` 块与丢弃清单，绝不换虚假 stub) | ✅ 生产实证 (1.4.0) |
| | 三段式渐进披露 (HMAC 游标签名，detail=index) | ✅ 生产实证 (1.4.0) |
| | 锚定通道 (Anchor Channel：L0 铁律与核心偏好免受相似度否决) | ✅ 生产实证 |
| **知识与人类端** | 声明式早报与模型探活探针飞轮 | 📋 v1.6.0 |
| | Obsidian 本地双向投影器与 Git 快照气囊 | 📋 v1.7.0 |
| | Mímir 2.0 Command Center 态势大盘 | 📋 v2.0.0 |

---

## 评估与基准测试 · Benchmarks

Mímir 严格恪守跑分三大纪律：**口径同行、到期兑现、连错题本一起交**。

**金标 24 条（生产活服务实测，全通道标准装配面）**：

| 指标 | 当前值 | 质量底线 | 状态 |
|---|---|---|---|
| **hit@1** | **0.750** | — | 顶层精准命中 |
| **hit@3** | **0.833** | 0.650 | ✅ 远超基准线 |
| **hit@5** | **0.875** | — | 极高召回面 |
| **hit@10** | **0.917 ~ 1.000** | 0.830 | ✅ 生产实证保底 |
| **MRR** | **0.786** | — | 排序质量优异 |

跑分执行入口：
```bash
# 运行金标评测（打活服务，语料在仓内）
python scripts/run_benchmarks.py --golden --out report.json
```

---

## 演进路线图 · Roadmap

| 版本 | 阶段代号 | 核心里程碑 | 状态 |
|:---|:---|:---|:---|
| **v1.3.0** | Ecosystem · 生态 | RECALL 漏斗面板、时序采集补齐、判重接通、Reflect 反思 | ✅ 已发布 |
| **v1.4.0** | Legibility · 可读性 | 召回解释机器可读面、三段式渐进披露 (HMAC 游标)、预算诚实协议 | ✅ 已发布 |
| **v1.4.1** | Extraction Fix | 抽取流水线双向打通，修复助手回复与工具实体遗漏 | ✅ 已发布 |
| **v1.5.0** | Federation & Sharing | 突破 84% 孤岛、退役 mentor、零配置动态 Agent 自举 | ✅ 已发布 · **当前生产运行** |
| **v1.6.0** | Entities & Synthesis | 结构化实体池、双时态时序失效、每日科技早报与模型探针 | 📋 计划中 (Issue #24) |
| **v1.7.0** | Obsidian Bridge | Obsidian 第五投影器、单向保护锁、本地 Git 隐形快照气囊 | 📋 计划中 (Issue #25) |
| **v2.0.0** | The Unified Brain | 统一 MCP/CLI 一键分发、Command Center 大盘、开源正式发布 | 🎯 终极目标 (Milestone #6) |

---

## 名字的由来 · The Name

源自北欧神话中的智慧巨人 **密米尔（Mímir）**。他看守着位于世界之树尤克特拉希尔根部的智慧之泉。众神之父奥丁曾为了喝一口智慧之泉的水，献出了自己的一只眼睛。

在现代 AI 多 Agent 协作系统中，Mímir 扮演着同样的守泉人角色：它不盲信任何模型的瞬时幻觉，守护着唯一的事件真相，让所有 Agent 都能在这里汲取经过严谨治理与审计的长久智慧。

---

## 许可证 · License

本项目采用 [MIT License](LICENSE) 开源许可。
