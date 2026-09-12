# Mímir — 面向多智能体系统的联邦记忆

> **一份共享记忆，多个智能体。** 一个事件溯源、自我演化、联邦化的记忆系统，
> 让多个 AI 智能体「一起」记忆——并智能地遗忘。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-20-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README_en.md) · [简体中文](README.md)

---

> ### 为什么选 Mímir · Why Mímir
>
> 市面上的记忆系统大多「**来者不拒**」：agent 说记就记，没有审批、没有账本、没有撤销。
> Mímir 从第一天起就是反着设计的——**每一条记忆都要过审、有账、可悔**：
>
> | 硬牌 | 说明 |
> |---|---|
> | **治理管线** | 新记忆先进候选队列，质量评估+人工确认才入库（全市场独一份） |
> | **事件溯源账本** | 谁记的、改过几版、谁批准的——从写入起全程可审计 |
> | **细粒度 ACL** | owner/可见性/外发策略三件套；联邦信封 Fernet 加密 |
> | **诚实遥测** | 查询带 `recall_verdict ∈ {found, not_found, degraded}`——「没查到」和「通道坏了」永不混淆 |
>
> 当前版本 **v14.2.1**（0.x 纪元收官在即）→ 下一版 **1.0.0 The Trust Baseline**，
> 进入 semver 承诺纪元（[版本纪元表](docs/VERSIONING.md)）。

---

## 快速开始（30 秒版）· TL;DR

```bash
# 一键初始化（含 bge-m3 模型预取）
./scripts/init.sh
# 启动 API（默认 127.0.0.1:8456，token 必备）
python -m mimir_v8.server --data-dir ./var/mimir-v8
# Claude Code / 任何 MCP 宿主接入
claude mcp add mimir -- mimir-mcp    # 27 个 mimir_* 工具
cp -r skills/mimir ~/.claude/skills/ # 场景路由 skill
```

详细路径见下文「快速开始（开箱即用）」与 [skills/mimir/INSTALL.md](skills/mimir/INSTALL.md)。

---

## 名字的由来 · The Name

**Mímir**（密米尔）源自北欧神话 —— 智慧之泉（Well of Mímir）的守护者。

在北欧神话中，众神之父奥丁（Odin）为了换取一口智慧之泉的井水，献出了自己
的一只眼睛。而这口泉水之所以蕴含智慧，正是因为它由巨人 **Mímir** 日夜守护——
Mímir 本身就是「记忆」与「知识」的化身。奥丁失去一只眼，却得到了预见未来的
智慧；而 Mímir 的头颅，即使在诸神黄昏（Ragnarök）之后，仍被奥丁带在身边，
继续为他提供忠告。

这个名字对一套记忆系统而言，是一个恰到好处的隐喻：

- **记忆需要代价** —— 奥丁用一只眼换智慧，正如可靠的记忆需要投入治理、
  审计与演化的成本，而非廉价的「记下来就行」。
- **记忆是长存的** —— 即使世界毁灭（诸神黄昏），Mímir 仍在。真正的记忆
  系统应当经得起时间的冲刷、版本的更迭，而不是随进程重启而消失。
- **记忆的价值在于「被喝下」** —— 泉水若无人饮用便只是水。记忆若无法在
  正确的时刻、以正确的权限被检索到，就只是堆积的数据。

Mímir 因此不只是一个技术命名，而是一句设计承诺：**做一套值得用「一只眼睛」
去换的、长存的、可饮用的记忆。**

---

## 我们的哲学 · The Philosophy

Mímir 建立在一个简单但常被忽视的信念上：**记忆不是数据的堆积，而是一个
有生命周期的过程。**

大多数记忆系统把「记住」当成终点——存进去，取出来，完事。但真实世界的
记忆不是这样的。真实记忆会：

1. **被审慎地接纳** —— 不是所有信息都值得记住，也不是所有「记住了」都
   该被无条件信任。所以我们让每一条候选在进入记忆之前，都经过治理评估，
   并让「提取」与「批准」永远分离。
2. **随时间演化** —— 用得多的记忆更可信，失效的记忆被降权。记忆的
   置信度应该是一段随时间变化的曲线，而不是一个静止的数字。
3. **懂得遗忘** —— 遗忘不是记忆的敌人，而是记忆的一部分。真正的遗忘
   是「有选择地放下」，而不是「销毁」。所以我们用墓碑标记而非删除，
   用艾宾浩斯曲线而非一刀切。
4. **有归属、有边界** —— 在多个智能体共享的世界里，「谁记得」和
   「谁能看」与「记住了什么」同样重要。记忆必须有主人，有边界，有
   克制的共享。

这就是 Mímir 的全部哲学：**把记忆当作一件需要被尊重、被治理、被演化、
被有意识地遗忘的事物，而不是一个可以无限写入的哈希表。**

我们不追求「记得最多」，我们追求「记得恰到好处」。

---

## Mímir 独一无二的能力：**联邦记忆**

大多数记忆系统为**单个智能体**设计。Mímir 为**多个智能体**设计。

在多智能体系统中——网络运维智能体、量化投顾智能体、技术顾问、培训师——每个
智能体职责不同、知识不同、归属不同。它们不该看到所有内容，但**应该**能共享
真正重要的东西。

Mímir 的答案是**细粒度隔离的联邦记忆**：

- **每个智能体有自己的记忆**——事实用 `owner_principal` 打标签，ACL 精确控制
  谁能读什么。
- **智能体有意识地共享**——三档可见性（`all` / `shared` / `owner_only`）让你把
  事实标记为「仅我自己」「我的团队」或「所有智能体可见」。
- **跨智能体感知**——感知广播呈现其他智能体最近学到了什么，让智能体不再各自为战。
- **联邦搜索**——`/v10/federation/{peer}` 在 ACL 约束下跨主体查询，让一个智能体
  能安全地问「有没有谁知道关于 X 的事？」。

结果：**N 个智能体共享一份记忆底座，同时保有 N 份私有记忆的隔离性。** 这就是
「记忆存储」与「集体记忆」的区别。

---

## Mímir 的其他与众不同之处（联邦之外）

联邦记忆是招牌。但 Mímir 也建立在一个与「带 API 的向量数据库」根本不同的前提上：
**记忆是事件，不是一行数据。**

| 普通的记忆存储 | Mímir |
|---|---|
| 为单个智能体设计 | **为 N 个智能体设计，ACL 隔离的联邦** |
| 覆盖旧记忆 | **追加不可变事件**——历史永不改写 |
| 「遗忘」= 删行 | **墓碑遗忘**——标记，而非销毁 |
| 记忆质量靠你的 prompt | **可治理**——LLM *评估*每个候选；只能*建议*，不能*提交* |
| 静态检索分 | **自我演化**——反馈让置信度可升可降 |
| 单一向量索引 | **三通道融合**——向量 + 全文 + 图，RRF + 本地重排 |
| 事实随意衰减 | **艾宾浩斯衰减**——六级层级（L0 永不遗忘 + 五条遗忘曲线） |

Mímir 是集体智能体记忆的完整生命周期：
*摄入 → 治理 → 提交 → 检索 → 自我纠偏 → 遗忘*——每一步可审计、可回滚。

---

## 一图看懂核心架构

```
                         ┌─────────────────────────────┐
   对话 / 采集 ────────▶ │          治理层             │
                         │  候选 → LLM 评估 →          │
                         │  噪声 / 暂定 / 人工审核 /    │
                         │  提交                     │
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │  规范化存储（事件溯源）      │
                         │  facts + memory_events +     │
                         │  fact_versions（不可变）      │
                         │  owner_principal + ACL       │
                         └──────────────┬──────────────┘
                                        ▼  （outbox 扇出）
              ┌──────────────┬──────────┴──────────┬──────────────┐
              ▼              ▼                     ▼              ▼
          向量 (chroma)   全文 (FTS5)           图             核心记忆
              └──────────────┴──────────┬──────────┴──────────────┘
                                        ▼
                              RRF 融合 + 本地重排
                                        ▼
                          排序后、经 ACL 过滤的结果
                          （逐智能体可见性强制）
```

---

## 六大支柱

### 1. 联邦记忆（多 Agent 联邦记忆）— *招牌能力*
多个智能体共享一份记忆底座，`owner_principal` 隔离、三档可见性、跨智能体感知、
带 ACL 的联邦搜索。多智能体接入指南见 [docs/FEDERATION.md](docs/FEDERATION.md)。

### 2. 事件溯源真相（事件溯源）
每条事实都是追加式事件流。`memory_events` 和 `fact_versions` 受触发器保护，
拒绝 UPDATE 和 DELETE——可回放、可审计、可解释，是结构属性。

### 3. 治理闭环（受控的摄入）
确定性规则引擎 + 独立的 LLM 评估器在提交前分类每个候选。LLM 被**刻意与提交路径
分离**——不能既提取又批准。

### 4. 对称自我演化（检索自进化）
检索反馈（`有用`/`无用`/`纠正`）按 7 天窗口聚合，置信度可升可降，受最小信号数
门槛约束。

### 5. 科学的遗忘（艾宾浩斯曲线）
六级艾宾浩斯衰减层级（L0_never + 五条遗忘曲线）+ Chronos 双时间轴。身份规则永不衰减，临时事实 7 天半衰期，
过期事实被降权——永不删除。

### 6. 本地优先隐私（本地隐私）
所有嵌入（bge-m3）和重排（ms-marco）在**本地 CPU** 运行——待嵌入文本绝不出机器。
API 仅绑定 `127.0.0.1`。

---

## 快速开始（开箱即用）

**一条命令** —— 安装依赖、生成配置与 token、并启动服务：

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
./bootstrap.sh
```

就这么简单。`bootstrap.sh` 做了三件事：
1. `pip install -e ".[embeddings]"` —— 安装依赖（首次运行会下载 bge-m3 模型）
2. `scripts/init.sh` —— 生成目录、agent token、最小配置
3. 在 `127.0.0.1:8456` 启动服务

然后访问 `curl http://127.0.0.1:8456/health` 确认。

> **手动安装**（如果你偏好自行控制）：
> `pip install -e ".[embeddings]"` → `./scripts/init.sh` → `python -m mimir_v8.server ...`
>
> **多智能体联邦接入**，按 [docs/FEDERATION.md](docs/FEDERATION.md) 操作。

---

## 能力矩阵

| 能力 | Mímir |
|---|---|
| **多智能体联邦记忆 + ACL 隔离** | ✅ |
| 跨智能体感知广播 | ✅ |
| 联邦跨主体搜索 | ✅ |
| **跨节点 CRDT 联邦（Lamport LWW + Fernet 信封）** | ✅ 生产实证 2026-09-12 · production-proven（沙箱 11/11 → Tailscale 真双节点逐键全等 → 生产单向实弹；三段式证据链见 CHANGELOG 1.0 条目） |
| 事件溯源（不可变事件）| ✅ |
| 治理管线（LLM 评估器）| ✅ |
| 向量 + 全文 + 图融合（RRF）| ✅ |
| 本地 CPU 嵌入与重排 | ✅ |
| 检索反馈自我演化 | ✅ |
| 艾宾浩斯衰减 + Chronos 双时间轴 | ✅ |
| L0~L3 分层记忆与渐进展开 | ✅ |
| 锚通道（铁律与核心偏好免被相似度否决）| ✅ |
| 共享工作黑板（蒸馏成事实）| ✅ |
| 时态知识图谱（valid_during 历史）| ✅ |
| 主动意图前置唤醒 | ✅ |
| 冲突消解（标记争议，永不删除）| ✅ |
| **Mímir-Eval：标准化基准套件（HitRate@K · MRR · ACL 泄漏率、金标地板）** | ✅ |
| 技能结晶 | ✅ |
| **AutoSkill：痕迹 → Wiki → L3 技能自动编译** | ✅ |
| **跨模型投影（档位感知注入块）** | ✅ |
| 多模态事实资产 | ✅ |
| Obsidian wikilink 双向链接 | ✅ |
| MCP 服务（27 工具）| ✅ |
| Hermes MemoryProvider 插件 | ✅ |
| Dashboard（13 标签页 Web 界面）| ✅ |
| PyPI + Docker 打包 | ✅ |

---

## 版本史边界 · Pre-Open-Source Era

公开 git 历史与 tag 自 **v12.0.0（2026-08-18 开源首发）** 起；v9~v11 为内部时代，
无公开 tag（快照与内部史不随开源导出——历史在 [CHANGELOG](CHANGELOG.md) 文本承载）。

## 路线图

| 里程碑 | 范围 | 状态 |
|---|---|---|
| v10.0 | 包内治理、Opinion/Observation 置信度层 | ✅ 已发布 |
| v11.0 | 符号短时记忆 + CodeGraph + reflect/federation API | ✅ 已发布 |
| v12.0 | Insight：艾宾浩斯衰减、Chronos、EvolveMem、召回漏斗、冲突消解、技能结晶、MCP、多模态、PyPI/Docker | ✅ 已发布 |
| v12.1 | Mímir-Eval 基准套件（HitRate@K/MRR/ACL 泄漏率、金标地板）+ 全源采集 + 动态注册表 | ✅ 已发布 |
| v12.2 | L0~L3 分层记忆、统一 Profile API、XTMEM 血缘、锚通道 | ✅ 已发布 |
| v13.0 | 多智能体共享黑板、时态知识图谱、主动意图唤醒 | ✅ 已发布 |
| v14.0 | AutoSkill 流水线、跨节点 CRDT 联邦、跨模型投影 | ✅ 已发布 · **2026-09-03 起在生产运行**（联邦协议层实现于 v14.0，但**双节点同步尚未在生产通电**——见功能矩阵标注） |
| v14.1.0 | 审计修复：治理 LLM 静默失效根治、韧性挡位（三态断路器）、诚实遥测、离机备份、看板 v4 客户视图 | ✅ 已发布 · **2026-09-08 起在生产运行** |

---

## 安全

- API 仅绑定 `127.0.0.1`；远程访问经反向代理（nginx / Cloudflare Tunnel）
- 每个端点 Bearer token 鉴权 + 权限范围（read/write/review/manage/admin）
- SQLite 触发器保证 `memory_events` / `fact_versions` 不可变
- 所有变更携带幂等键 + `actor_principal` + 审计日志
- `egress_policy=local_only` 阻止敏感事实被外部处理

漏洞披露政策见 [SECURITY.md](SECURITY.md)。

---

## 鸣谢

Mímir 站在多个优秀开源记忆项目的肩膀上。我们衷心感谢它们的作者：

| 项目 | 作者 | 我们学到了什么 |
|---|---|---|
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | 塑造 Mímir v12「Insight」的**治理 + 自我演化愿景**：Tahoe-Gate 相关性门控、EvolveMem 反馈回路、冲突消解、技能结晶。对我们设计影响最大的单一项目。 |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | 腾讯云 | 符号短时记忆（Mermaid 画布卸载 + 下钻）与 CodeGraph 索引 |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | 信念建模——区分「我知道什么」与「我有多大把握」的 Opinion/Observation 层 |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | 记忆管线范式：分层存储、上下文管理、记忆作为一等公民服务 |

**特别致意 [aiduMEI](https://github.com/monkey2jack/aiduMEI)**（aidu Memory
Engine Insight，「爱嘟优忆思」）：除了上述四个借鉴模式，其作者关于**原文保真 vs
蒸馏**的深刻思考——「蒸馏会丢温度，原文才是证据」——直接启发了 Mímir 的保留豁免
设计：被已提交事实引用的对话消息永不被清理。我们诚心推荐你去了解 aiduMEI。

---

## 环境变量速查 · Environment Variables

全部 `MIMIR_*` 环境变量约 56 个，核心面如下（完整清单以 `grep -rhoE 'MIMIR_[A-Z_0-9]+' mimir_v8/` 为准——本表是语义速查不是穷举）：

### 路径与身份

| 键 | 语义 |
|---|---|
| `MIMIR_HOME` / `MIMIR_DATA_DIR` / `MIMIR_CACHE_DIR` / `MIMIR_SECRETS_DIR` / `MIMIR_LOG_DIR` | 主目录/数据/缓存/密钥/日志五路径（生产 unit 全显式设置；`MIMIR_V8_DATA_DIR` 为历史别名同指数据目录） |
| `MIMIR_AGENTS` / `MIMIR_DOMAINS` | 启动期扩册清单（逗号分隔，v14.2 新增；四席默认之上追加） |
| `MIMIR_ALLOW_NONLOOPBACK` | 跨网卡监听开关（默认 0，容器部署经 compose 显式授予） |

### 「看似重复实为分工」的两对键（文档缺位曾致误判）

| 键 | 语义 | 谁读 |
|---|---|---|
| `MIMIR_HERMES_STATE_DB` | CDC 抽取的 Hermes 状态库（worker `--state-db` 的 env 形态） | `worker.py:46` |
| `MIMIR_CONNECTOR_HERMES_STATE_DB` | 同一库的 config 面引用（`config.py` 装配 `MimirPaths`） | `config.py:39` |
| `MIMIR_EVAL_API` | **自评套件**的 Mímir API 地址（Mímir-Eval 打分用） | `eval_suite.py:56` |
| `MIMIR_EVAL_API_URL` | **评估器 LLM** 的网关地址（治理候选打分用） | `evaluator.py:25` |

> 两对都是活键、管不同的事——HERMES_STATE_DB 双名是历史层积（worker CLI 与 config 两条装配线），EVAL 双名是自评与治理两个消费方。改名属破坏性变更，暂留双名+文档锚定。

### LLM 与治理

| 键 | 语义 |
|---|---|
| `MIMIR_EVALUATOR_API_KEY` / `MIMIR_EVALUATOR_API_URL` / `MIMIR_EVALUATOR_MODEL` | 评估器 LLM 三件套（治理候选打分） |
| `MIMIR_ROUTER_URL` / `MIMIR_ROUTER_API_KEY` | 9router 路由面（部分组件的 LLM 出口） |
| `MIMIR_GOVERNANCE_MODEL` / `MIMIR_GOVERNANCE_FALLBACK_MODEL` | 治理主/备模型 |
| `MIMIR_GOVERNANCE_AUTO_APPROVE` | fast_track 总闸（**v14.2 起默认 0**；生产 unit 显式 =1） |
| `MIMIR_FAST_TRACK_THRESHOLD` | fast_track 置信度阈值（默认 0.8） |
| `MIMIR_LLM_SALIENCE_THRESHOLD` / `MIMIR_EXTRACTION_LIMIT` | 抽取链参数 |

### 客户端与检索

| 键 | 语义 |
|---|---|
| `MIMIR_V8_URL` / `MIMIR_V8_TOKEN` / `MIMIR_V8_TIMEOUT` | API 客户端三件套 |
| `MIMIR_V8_TOKEN_FILE` / `MIMIR_V8_CLIENT_TOKEN_FILE` | 服务端 token 表 / 客户端 token 文件 |
| `MIMIR_V8_COLLECTION` / `MIMIR_V8_MODEL` | Chroma collection 名 / 嵌入模型名 |
| `MIMIR_V9_KNOWLEDGE_LAYERS` | 知识层开关（memory,learning,wiki） |
| `MIMIR_VAULT_ROOT` | Obsidian vault 根（采集器） |

## 版本号域表 · Version Domains

多个版本号并存不是漂移——五个域各有语义，彼此独立演进（嘟嘟审计 🟡7 的澄清）：

| 域 | 当前值 | 语义 | 在哪改 |
|---|---|---|---|
| **Release 版本** | `14.2.0` | 功能发布号（`MIMIR_VERSION`） | `mimir_v8/schema.py`——单一事实源，`pyproject.toml` 与 CI 对拍断言强制同步 |
| **Schema 版本** | `20` | 数据库结构代数（迁移链盖章） | `mimir_v8/schema.py::SCHEMA_VERSION`——变更必须配迁移链 |
| **API 代数** | `v8`~`v13` | 端点路径前缀（`/v8/query`、`/v9/search-preview`、`/v12/search/trace`、`/v13/blackboard`）——保留历史代数是兼容承诺 | `mimir_v8/api.py` |
| **包名** | `mimir-v8` | PyPI/包管理名（历史命名，函数性冻结） | `pyproject.toml` |
| **仓库名** | `mimir-memory` | GitHub/Gitee 仓库名 | 平台侧 |

> 改 Release 版本时必跑 `tests/test_p0o_version_domains.py`（三方对拍：schema.py ↔ pyproject.toml ↔ CHANGELOG 版本段）。

## 许可证

[MIT](LICENSE)

## 联系方式

维护者：**sandro1123** · 📧 [sandro1123@hotmail.com](mailto:sandro1123@hotmail.com)
