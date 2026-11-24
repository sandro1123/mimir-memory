# Mímir 角色与身份清单（2026-11-24 实测）

> 来源：`api_tokens-v8.1-prod.json`（生产 token 注册表，只读，只看 id 和 scopes）、`conversation_sources`、`candidate_facts`、宿主机 hook 配置、进程列表。
> 原则：**一个 agent 只对应一个身份；agent 不能审核自己的候选；共享只靠读取可见性，不靠身份等价。**

## 1. 现状

| 角色 | 实际用的 principal | 采集方式 | 当前 scopes | 问题 |
|---|---|---|---|---|
| Heimdallr | heimdallr | Hermes 全局 CDC | read / write / delete | — |
| Jarvis | jarvis | Hermes profile CDC | read / write / delete | — |
| QuantMaster | quantmaster | Hermes profile CDC | read / write / delete | — |
| Mentor | mentor | 历史 CDC（10-23 起停更）；RSS 与 Vault 采集也记在它名下 | read / write / delete / ingest / **review** | 采集器借用了人格身份 |
| **Thor**（飞书 aiduCAT 桥，n100） | **claudecode** | `~/.claude/hooks/mimir_auto_capture.py`（默认 `AGENT_ID=thor`） | read / write / delete / ingest / **review** | 冒用 claudecode；能审核自己的候选 |
| **Odin**（sanjose） | **claudecode** | `mimir` CLI（`~/.mimir_claudecode.token`） | 同上 | 冒用 claudecode |
| Claude Code（本机 CLI 会话） | claudecode | 同一个 hook | 同上 | 和 Thor 分不开 |
| 治理服务 | service:governance | — | 系统内审 | — |
| admin（您本人） | admin | — | 全部 | — |
| kilo_bridge.py | 未知 | 未知 | — | 待确认归属 |

## 2. 目标

| 角色 | principal | scopes | 读取可见范围 |
|---|---|---|---|
| Thor | `thor` | read, ingest | 自己的 + 团队共享 |
| Odin | `odin` | read, ingest | 自己的 + 团队共享 |
| Claude Code | `claudecode` | read, ingest | 自己的 + 团队共享 |
| Heimdallr / Jarvis / QuantMaster | 维持现状 | read, write（delete 后续评估） | 不变 |
| 采集器（RSS / Vault） | `service:collector`（后续拆出） | ingest | — |
| 治理服务 | `service:governance` | review（系统） | — |
| 您 | admin | 全部，唯一的人工审核身份 | — |

## 3. 改动要点（1.9.0 第 1 步）
1. 签发 `thor`、`odin` 两个 principal，只给 read 和 ingest，token 放到 `~/.hermes/mimir/secrets/clients/`。
2. 收回 claudecode 的 review、delete、write，只留 read 和 ingest（您已同意）。
3. `PRINCIPAL_ALIASES` 不再让 thor/odin/claudecode 和 mentor 等价，改成**团队共享只读**。写入和审核时一律用自己的身份，不继承别人的。
4. hook 和 CLI 必须显式指定身份；缺 `MIMIR_AGENT_ID` 或对应 token 时**报错退出**，不再默认当成 thor 或 claudecode。
5. 保留现有 thor-bridge 和 odin-bridge 数据的 `connector_id`；历史记录的 `owner_principal` 不做回写。
