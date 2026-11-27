# Mímir 角色与身份清单（2026-11-24 更新，1.9.0）

> 来源：`api_tokens-v8.1-prod.json`（生产 token 注册表，只看 id 和 scopes）、`conversation_sources`、宿主机 hook 与 CLI 配置。
> 原则：**一个 agent 只对应一个身份；agent 不能审核自己的候选；共享只靠读取可见性，不靠身份等价。**

## 1. 现状（1.9.0 之后）

| 角色 | principal | 采集方式 | scopes | 备注 |
|---|---|---|---|---|
| Heimdallr | heimdallr | Hermes 全局 CDC | read / write / delete | — |
| Jarvis | jarvis | Hermes profile CDC + 插件召回（profile `jarvis`） | read / write / delete | **隔离租户（1.9.5）**：与机群双向不共享 |
| QuantMaster | quantmaster | Hermes profile CDC | read / write / delete | — |
| Mentor | mentor | 历史 CDC；RSS 与 Vault 采集也记在它名下 | read / write / delete / ingest / review | 采集器仍借用人格身份，待拆 `service:collector` |
| Thor（飞书 aiduCAT 桥 + n100 本机 Claude Code） | `thor` | `~/.claude/hooks/mimir_auto_capture.py`（SessionStart / Stop / SessionEnd） | ingest / read / write / review | token：`secrets/clients/thor.token`；review 仅用于执行用户已批准的审核（1.9.2） |
| Odin（sanjose） | `odin` | `~/.claude/hooks/mimir_auto_hook.py`（SessionStart / Stop / SessionEnd / PreCompact） | ingest / read / write | token：`~/.mimir_odin.token` |
| Brokkr（Gungnir，pi） | `brokkr` | `adapters/pi/mimir.ts`（session_start / compact / agent_settled） | ingest / read / write | `MIMIR_AGENT_ID=brokkr` 必须显式；token `~/.mimir_brokkr.token`（1.9.3） |
| Mirasim 采集器（n100 定时拉取 X1E） | `mirasim` | `adapters/mirasim/collector.py`（`mimir-mirasim-capture.timer`，10 分钟） | ingest | token `secrets/clients/mirasim.token`；不读不写（1.9.3） |
| 治理服务 | service:governance | — | 系统内审 | — |
| admin（您本人） | admin | — | 全部 | 审核决定由您批准；Thor 以自己的身份执行并在 reason 中记录批准 |

`claudecode` 已于 2026-11-24 退役：facts 按来源拆给 thor / odin，token 已吊销。CLI 与 hook 不再回落到它。

**身份必须显式（1.9.1）**：Claude Code hook 只认 `MIMIR_AGENT_ID`，不再按 `LARK_CHANNEL` 猜（Odin 也跑在 lark bridge 下，旧逻辑会把它认成 thor），也不再默认 `thor`。未设置时 hook 记一条错误、不发任何请求、exit 0。
- n100（Thor）：`~/.claude/settings.json` 里三个 hook 命令前加 `MIMIR_AGENT_ID=thor `，**必须先改再装新 hook**，否则 Thor 停止沉淀。
- sanjose（Odin）：`MIMIR_AGENT_ID=odin`，token `~/.mimir_odin.token`（`MIMIR_TOKEN_FILE`）、`MIMIR_HOST=<n100-tailscale-ip>`。11-25 起由本地包装 `mimir_auto_hook.py` 固定这三项；新 hook 装好后可改为 settings 前缀并去掉包装。

**隔离租户（1.9.5）**：Jarvis 服务机群以外的人。"不进团队共享名单"并不等于隔离——技术域共享对所有已认证身份
开放，所以 1.9.5 起由 `ISOLATED_PRINCIPALS` 在服务端双向封住：技术域共享、团队共享、role 授权都不跨越边界，
只有显式 principal 授权或 admin 可以。Hermes 插件按 profile 取身份（`default→mentor`、`jarvis→jarvis`、
`quantmaster→quantmaster`），缺身份或 token 不发请求，不再兜底 mentor / admin。

## 2. 与原目标的差异
- thor / odin 目前有 `write`：`mimir-remember` 走 `POST /v8/facts` 需要它。原目标是只留 read + ingest，是否收回待定（收回后 remember 要改走 ingest 通道）。
- 采集器拆身份（`service:collector`）未做，留到后续版本。

## 3. 规则
1. 写入和审核一律用自己的身份；`PRINCIPAL_ALIASES` 只提供团队共享只读。
2. token 文件按身份分开，权限 0600；找不到自己的 token 时报错退出，不借用别人的。
3. 新 agent 接入：注册 principal → 签发 token → 加入 `MIMIR_AGENTS` → 加入 `capture-check` 监控列表。
4. 历史数据的 `connector_id`（thor-bridge / odin-bridge）保留，不回写。
