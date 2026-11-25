# Mirasim 对话自动采集 · 设计（2026-11-25，待批准）

> 状态：**设计稿，未实施**。批准后才写代码。
> 背景：Mirabot 是 Mirasim（X1E 上的宿主程序）内置的主助手（`assistants.json` 里 `id=matrix`）。Mirasim 没有 hook，但会把所有聊天完整存成本地文件。本方案定时读取这些文件，送进 Mímir。

## 1. 一句话
n100 上加一个定时任务，每 10 分钟通过局域网 SSH 读取 X1E 上 Mirasim 的聊天日志，只取新增的消息，打包后调用 Mímir 现有的 `POST /v8/ingestion/conversations`，然后照常走 抽取 → 候选 → governance 审核。**不在 X1E 上装任何东西，不改 Mirasim，只读。**

> 与之前口头说的"在 X1E 上放小程序"不同：改成由 n100 去拉取。这样 token 不用放到笔记本上，也不用在 Windows 上装 Python 和计划任务，管理方式也和其它定时任务一样。

## 2. 数据来源（已在 X1E 上只读核实）
- 目录：`C:\Users\sandr\.mirasim\chat\rooms\<room>\log\<seq>.jsonl`。每行一个 JSON，只追加不改写，`seq` 在房间内递增；文件名是该文件第一条的 seq。
- 行类型：`room`（房间信息）、`system`（系统事件）、`message`（消息：`seq, ts, by, name, via, kind, text, card, cid`）。**只采集 `message`。**
- 发言人：`users/<uid>/profile.json` 的 `kind` 是 `human` 或 `agent`，据此决定 role。
- 现有 10 个房间：7 个助手私聊（`dm-ai-*`）、`dm-mx-matrix`、`general`、`items`。

## 3. 映射到 Mímir
| Mímir 字段 | 取值 |
|---|---|
| `connector_type` | `external_agent` |
| `connector_id` | `mirasim-x1e` |
| `owner_principal` | `mirasim`（新身份，见 §5） |
| `session_id` | 房间名 |
| `messages[].role` | 人 → `user`，助手 → `assistant` |
| `messages[].content` | `text`；卡片只取其中的文字 |
| `messages[].created_at` | `ts` |
| `messages[].metadata` | `room`、`seq`、发言人名字、助手 id |
| `memory_mode` / `retention_class` | `observe` / `short`（只进候选，不直接入库） |
| `idempotency_key` | `mirasim:<room>:<首seq>-<末seq>`，重复发送不会重复入库 |

每批最多 200 条（接口上限 1000）。

## 4. 运行细节
- **进度记录**：n100 `~/.hermes/mimir/state/mirasim_cursor.json`，按房间记已处理到的 seq。只有接口返回成功后才前移。
- **10 分钟冷静期**：只采集 10 分钟以前的消息，给撤回和编辑留出时间。撤回发生在冷静期之后的，记日志，v1 先人工处理。
- **断线补齐**：X1E 关机或不在家时本轮跳过；日志只追加，下次连上会自动补齐，不会丢。
- **读取量**：只读取 seq 不小于进度的日志文件，已完结的旧文件不再读。
- **先演练**：带 `--dry-run` 参数时只打印将要发送的内容，不调用接口。

## 5. 身份与权限
- 新建身份 `mirasim`，**只开 `ingest`**（不开 read/write/review）。token 放在 n100 的 `secrets/clients/mirasim.token`（0600），不放到 X1E。
- 按 ROLES.md 第 3 条走完四步：签发 → `register-agent mirasim` → `MIMIR_AGENTS` → `capture-check` 加上 `mirasim-x1e=24h`。
- Mirabot 以后如果要通过 MCP **读取** Mímir，再另发 `mirabot` 身份，本方案不涉及。

## 6. 代码与部署
- 代码：`adapters/mirasim/collector.py`，附测试（用假的 jsonl 样本测：只取 message、进度、幂等、冷静期、卡片取文字、批量切分）。
- 部署：`mimir-mirasim-capture.service` 和 `.timer`（每 10 分钟），需要 `daemon-reload`。
- 走"分支 → 测试 → PR → 合并"，同步到 GitHub。

## 7. 回滚
- 停止并禁用 timer，采集立即停止，X1E 和 Mirasim 不受任何影响。
- 已进入 Mímir 的内容都带 `connector_id=mirasim-x1e`，可按来源追溯。**按来源批量清除的现成工具还没确认是否存在**，实施时先核实，没有就补上。

## 8. 需要你定的两件事（下面是默认值）
1. **历史补录**：默认只补**最近 7 天**。全部补录会让候选队列一下子涌入很多条。
2. **采集哪些房间**：默认采 7 个助手私聊、`dm-mx-matrix` 和 `general`，**不采 `items`**（任务事项，不是对话）。

## 9. 本方案不做
- 不碰 Mirasim 自带的 `assistant-memory/*.md`（它和 Mímir 功能重叠，谁为准以后再定）。
- 不做 MCP server。
- 不修 Tailscale 直连（现在走局域网）。
