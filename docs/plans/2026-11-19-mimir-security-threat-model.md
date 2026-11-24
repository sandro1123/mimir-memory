# Mímir 安全与隐私威胁模型（P1 草案）

> **日期**：2026-11-19  
> **状态**：P1 草案（威胁识别 + 最小控制面 + 与不变量映射）  
> **关联**：`docs/INVARIANTS.md`（INV-04/08）、`mimir_v8/learning.py`（DLP）、`mimir_v8/api.py`（鉴权/ACL）

---

## 1. 资产与信任边界

| 资产 | 说明 | 主要风险 |
|------|------|----------|
| 规范事实库（canonical facts） | `facts`/事件账本/谱系 | 越权读写、篡改、错误合并 |
| 候选与 STM/会话内容 | `candidates`、episode/ingestion payload（经 DLP） | 敏感信息残留、跨会话泄露 |
| 凭据与配置 | API token、`~/.claude`/`~/.codex`/`~/.mirasim` 中的密钥与 profile | 安装器/日志泄露、误提交 |
| 适配器注入面 | hooks、MCP 配置、settings 合并 | 配置投毒、权限提升、供应链脚本 |
| 审计与遥测 | `memory_events`、日志、trace | 篡改/缺失导致不可追责 |

**信任边界**：  
- Agent/CLI/适配器 ↔ Mímir API（Bearer + scope + owner/ACL）  
- 本地配置文件 ↔ 安装/合并工具（必须最小写入、可回滚）  
- 本地存储 ↔ 外部导出/Vault/发布（受 `egress_policy`/sensitivity/redact 约束，目标态）

---

## 2. 威胁与控制措施

### T1. 越权访问与串库（跨 principal / 跨 domain / 跨 session_scope）

- **现状控制**：`TokenStore` + scope（read/write/manage/review/ingest 等）、`owner_principal` 过滤与 hydrate 时 ACL 校验（`api.py` 多处）。  
- **缺口**：知识消费新工具（entity/runbook）未落地时契约未冻结；session_scope 归因在多宿主下需统一。  
- **要求**：所有新端点/工具必须声明最小 scope 与归属字段；权限拒绝必须审计（INV-04）。  
- **验证**：权限矩阵测试、异 principal 读写负例。

### T2. 敏感数据泄露（记忆内容、日志、Vault 发布、导出）

- **现状控制**：`learning.py` 对 conversation 消息做 DLP redaction（Bearer/`api_key|token|secret|password`/私钥块）；`retention_class`/`memory_mode` 约束摄入意图。  
- **缺口**：出口策略 `egress_policy` 的端到端强制（发布/Vault/外部推送）需在 1.7/1.9 收口；trace/日志可能携带敏感片段。  
- **要求**：默认 `local_only`；对外路径必须经 redact/准入判据；日志脱敏。  
- **验证**：发布准入用例、日志扫描、密钥 pattern 回归。

### T3. 配置与供应链投毒（hooks/MCP/settings 合并）

- **现状观察**：本机 `~/.claude/settings.json` 含 hooks 与 MCP；`~/.codex/config.toml` 含 MCP servers；`~/.mirasim/` 存在 setting/schedules 等——适配器安装面真实存在。  
- **缺口**：无统一“声明式安装器”契约与原子回滚保证；Mirasim L1 合并逻辑尚未实现。  
- **要求（INV-08）**：安装器仅改白名单字段；**不得读取/回显**凭据与 `auth.token` 等密钥；原子写 + 备份 + `--rollback`；dry-run diff。  
- **验证**：对含密钥配置的合并测试；补丁/输出密钥扫描。

### T4. 提示/记忆注入与污染（malicious content 入库或误召回）

- **现状控制**：候选评审、治理链路、可信度/衰减；自动输入止于候选（INV-05）。  
- **缺口**：自动触发扩大后注入面增大；热度/反馈若直连排序可能放大污染（回声）。  
- **要求**：高风险自动路径 shadow-only；反馈信号与排序解耦直至 Q4 实测通过；对来源/host/source 标注可追溯。  
- **验证**：注入语料回放集、shadow 漂移监控。

### T5. 审计缺失与不可抵赖

- **现状控制**：`memory_events` 账本、evolve 调整写事件、store 审计接口方向。  
- **缺口**：关键操作（拒绝、降级、安装器变更、迁移）需统一审计字段与留存策略。  
- **要求**：INV-01（删前有账本）；拒绝/降级可观测；迁移与开关变更记录 ADR/事件。  
- **验证**：故障注入下账本完整性检查。

### T6. 拒绝服务与资源滥用

- **现状控制**：`AuthBruteForceGuard`、`RequestSizeLimitMiddleware`、查询 limit/candidate_limit。  
- **缺口**：适配器高频摄入/trace 开销需限流与配额（按 adapter/principal）。  
- **要求**：摄入与 trace 接口具备速率/大小上限；后台任务可降级。  
- **验证**：压测与限流回归。

---

## 3. 数据分类与处理原则（最小集）

| 类别 | 示例 | 处理 |
|------|------|------|
| 高敏 | token/私钥/密码/个人隐私原文 | DLP 必须命中；禁止外发；日志脱敏 |
| 中敏 | 内部项目配置、会话上下文 | 默认 `local_only`；共享需 ACL + redacted 视图 |
| 低敏 | 公开知识、非敏操作记录 | 可按策略进入知识池/发布面 |

**原则**：最小权限、最小留存、显式 egress、可审计、可回滚。

---

## 4. 与路线图阶段的对应

| 阶段 | 安全交付 |
|------|----------|
| **1.9.0** | Contract 冻结含归因字段；安装器（Mirasim L1）满足 INV-08；权限/审计无回归 |
| **1.10** | egress/redact 准入落地到发布/导出路径；热度/反馈 shadow 与回声监控；SLO 告警 |
| **2.0** | 谱系/时态迁移的审计完整性；签名/可验证谱系（若纳入）走 ADR 与威胁复审 |

---

## 5. 待办（进入实现前细化）

1. 为 Mirasim `mcp.json` 合并器编写威胁用例：并发写、部分失败、回滚、密钥字段误匹配。  
2. 定义 trace/log 脱敏规则与采样策略（避免高敏 payload 入盘）。  
3. 明确 Vault Bridge 发布的“非敏文件镜像”判据与自动化检查点（对齐路线图 1.7 收尾项）。  
4. 建立安全回归清单并纳入 CI（密钥扫描、权限负例、DLP 样例）。