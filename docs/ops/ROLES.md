# Mímir 角色与身份清单（2026-11-24 更新，1.9.0）

> 来源：`api_tokens-v8.1-prod.json`（生产 token 注册表，只看 id 和 scopes）、`conversation_sources`、宿主机 hook 与 CLI 配置。
> 原则：**一个 agent 只对应一个身份；agent 不能审核自己的候选；共享只靠读取可见性，不靠身份等价。**

## 1. 现状（1.9.0 之后）

| 角色 | principal | 采集方式 | scopes | 备注 |
|---|---|---|---|---|
| Heimdallr | heimdallr | Hermes 全局 CDC | read / write / delete | — |
| Jarvis | jarvis | Hermes profile CDC | read / write / delete | — |
| QuantMaster | quantmaster | Hermes profile CDC | read / write / delete | — |
| Mentor | mentor | 历史 CDC；RSS 与 Vault 采集也记在它名下 | read / write / delete / ingest / review | 采集器仍借用人格身份，待拆 `service:collector` |
| Thor（飞书 aiduCAT 桥 + n100 本机 Claude Code） | `thor` | `~/.claude/hooks/mimir_auto_capture.py`（SessionStart / Stop / SessionEnd） | ingest / read / write | token：`secrets/clients/thor.token` |
| Odin（sanjose） | `odin` | `~/.claude/hooks/mimir_auto_hook.py`（SessionStart / Stop / SessionEnd / PreCompact） | ingest / read / write | token：`~/.mimir_odin.token` |
| 治理服务 | service:governance | — | 系统内审 | — |
| admin（您本人） | admin | — | 全部，唯一的人工审核身份 | — |

`claudecode` 已于 2026-11-24 退役：facts 按来源拆给 thor / odin，token 已吊销。CLI 与 hook 不再回落到它，缺身份时默认 `thor`，Odin 上显式用 `odin`。

## 2. 与原目标的差异
- thor / odin 目前有 `write`：`mimir-remember` 走 `POST /v8/facts` 需要它。原目标是只留 read + ingest，是否收回待定（收回后 remember 要改走 ingest 通道）。
- 采集器拆身份（`service:collector`）未做，留到后续版本。

## 3. 规则
1. 写入和审核一律用自己的身份；`PRINCIPAL_ALIASES` 只提供团队共享只读。
2. token 文件按身份分开，权限 0600；找不到自己的 token 时报错退出，不借用别人的。
3. 新 agent 接入：注册 principal → 签发 token → 加入 `MIMIR_AGENTS` → 加入 `capture-check` 监控列表。
4. 历史数据的 `connector_id`（thor-bridge / odin-bridge）保留，不回写。
