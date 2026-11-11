# GitHub 文字介绍包 · What the repo page should (and shouldn't) say

> 目的：把“文字面”（About 描述、Topics、README 首屏、pyproject、Releases 标题）与产品真相对齐。
> 原则：**只说可证伪的话**。禁止把 2.0 规格的“统一脑”写成已交付功能；所有承诺都必须能在代码或测试里找到落点（沿用 Mímir 自家判例：判据要在系统坏着时也通过，它就不是判据）。

---

## 1. 现状勘察（2026-11-12 实测）

| 面 | 线上现状 | 问题 |
|---|---|---|
| **About 描述** | `The trust baseline for multi-agent memory — event-sourced, governed, federated. 事件溯源 · 治理管线 · 联邦同步` | 只覆盖三支柱；看不见 1.7–2.0 的 Obsidian/自动记忆主线；“trust baseline”字面把定位钉死在“底座” |
| **Topics（9）** | ai-agents / event-sourcing / federation / knowledge-graph / llm / memory / multi-agent / rag / sqlite | 缺 `mcp`、`local-first`、`obsidian`、`event-sourced-memory`（检索词） |
| **首页链接** | `https://github.com/sandro1123/mimir-memory/releases` | 指向 Releases（尚可），但更应指向双语 README 的锚点 |
| **README 首屏** | 中英双语、徽章齐；功能表写到 2.0 的“双池一桥/第五投影器”为现货 | 2.0 未发布却被写成“核心特性”，与仓库实际状态不一致 |
| **Releases 标题** | `v1.6.0 — Perception & Crystallization — 实体知识库` | 与 README 的“当前生产版本”一致 ✅ |

---

## 2. About 描述（直接粘到 GitHub About 框，上限约 350 字符）

### 英文（推荐，检索覆盖优先）

```
Event-sourced, governed memory for multi-agent systems — candidate review pipeline, immutable audit ledger, ACL + federation, MCP server, honest budgeting. Local-first. Python + SQLite. MIT.
```

### 中文（若要保留双语气味）

```
事件溯源 · 治理管线 · 联邦共享 —— 面向多 Agent 的长时记忆底座（候选审批、不可篡改账本、ACL 隔离、MCP 接入）。本地优先，Python + SQLite，MIT。
```

> **修改纪律**：只在 GitHub Settings 里改 Description / Website / Topics；不要把介绍写进代码注释里改不动的东西。

---

## 3. Topics（全部替换，20 个内）

```
event-sourced-memory, multi-agent, agent-memory, mcp, mcp-server,
local-first, sqlite, knowledge-graph, federation, rag,
obsidian, claude-code, hermes-agent, codex, governance,
auditable, long-term-memory, ai-agents, python, mit-license
```

> 比现有 9 个多出：`mcp` / `mcp-server` / `local-first` / `obsidian` / `claude-code` / `long-term-memory` / `agent-memory` / `governance` / `auditable` / `hermes-agent` —— 直接对应检索词，不再等用户翻代码才发现 MCP。

---

## 4. 首页链接（Website 字段）

保持为 Releases 即可，但 Description 下要出现文档锚点：

```
https://github.com/sandro1123/mimir-memory#快速开始-60-秒版--quickstart-60-seconds
```

> 不要指向本地地址 `127.0.0.1:8456` —— 陌生访客跳过去只会看到连接拒绝。

---

## 5. README 修改清单（按文件）

### 5.1 `README.md` + `README_en.md`（首屏）

1. **功能表标题加状态列**：每行后面标注 `✅ 生产实证` / `📋 v1.7.0` / `🎯 v2.0.0 规格`，避免把未交付的“双池一桥”“Obsidian 第五投影器”当成现成能力。

2. **首屏加四行“能自动捕获 / 不会断链 / 不丢候选 / 有反推”**（其实是承诺，要在当前代码里能证伪）：
   - 自动捕获协议（`ingest_conversation` + 候选管线）
   - 断链不丢：confidence/degraded 不会被悄悄吞成 miss（`recall_verdict`）
   - 回来反推（`search_feedback` 读取 → 排名，待 1.8 落地前先只写“设计中”）

3. **新增《与其他记忆系统的差别》小节**（对标完后补，不许用“我们的可信底座更可信”这种空话）：只列结构性差异，如“候选先行而不是直接入库”“UPDATE 永不自动”“忘≠删”“检索热度不算搜过几次而是 attend 次数”。

### 5.2 `pyproject.toml`

把 `description` 出自当前滞后句式更新为 About 同语：

```toml
description = "Mímir — event-sourced, governed, federated memory for multi-agent systems (candidate→approval review, immutable ledger, ACL/federation, MCP server, honest budgeting). Local-first. Python + SQLite. MIT."
```

并补 `topics` 在 PyPI 侧可视化（已有的 `keywords` 追加 `mcp` / `obsidian` / `local-first`）。

### 5.3 `docs/GITHUB_INTRO.md`（本文件）

> 本文件是文字包的**单点真相**：GitHub About / Topics / 首页链接每改一次，同步更新本文件第 1–3 节和示例中的实测日期；之后不再三处散写。

### 5.4 Releases

- 1.7.0 打 tag 前，Releases 标题不要写“Obsidian Bridge 完成”；先用 “Stage-2 回流闭环 / 安全网 + 清册” 的真实进展。
- 每版 Release body 顶部沿用既有“合入说明 + 质量账本”，不要只复制 CHANGELOG。

### 5.5 `CONTRIBUTING.md` / `SECURITY.md` / `CODE_OF_CONDUCT.md`

- CONTRIBUTING 里 Bug 报告模板已给是 CI 可观测字段（版本/schema/`/health`），README 链接里加一句 “Bug/Patch 直接去 Issues（模板自动弹出）” 即可，别再扩字。
- SECURITY.md 补“报告通道”（GitHub Private Advisories 或邮箱）与“响应时限（72h Acknowledgement）”，这是合规侧的真缺口。

### 5.6 过程类文档边界（不要写进 README）

以下内容属于内部规划，不进 README 首屏，但应在 `docs/plans/` 自洽：

- 对标对照表（`docs/BENCHMARK-COMPARE.md`，1.8 启动前置）
- 自动记忆的 STM 冻结阈值、Adapter manifest 格式
- 双时态（valid/transaction）迁移步骤

---

## 6. 复核步骤（改完文字后）

1. GitHub 设置页确认：Description / Website / Topics 已为最终版。
2. `README.md` 与 `README_en.md` 渲染预览无坏图（徽章无 404）。
3. `pyproject.toml` 语法：`"/c/Program Files/Python310/python.exe" -m pip install -e . --no-deps` 不报错。
4. `python scripts/version_sweep.py` 无 STALE。
5. `ruff check mimir_v8/` 无告警（CI 硬失败）。

---

## 7. 本清单的信源

- GitHub API(2026-11-12)：About 描述、Topics 9 个、stars=5、homepage=Releases、默认分支 `master`、最新 Release `v1.6.0 (2026-10-26)`。
- 仓库实测：`README.md` 219 行、`README_en.md` ~186 行、`pyproject.toml` description 滞后、CONTRIBUTING/SECURITY/CODE_OF_CONDUCT 已存在。
