# Mirasim 适配 Spike 报告（P2：L1 / L2 / L3）

> **日期**：2026-11-19  
> **状态**：Spike 完成（基于本机只读实测 + 契约/原型设计；未改任何 `~/.mirasim/*`、`~/.claude/*`、`~/.codex/*` 生产配置）  
> **输入**：`docs/plans/2026-11-19-mimir-p0-real-baseline-audit.md`、`mimir_v8/adapters/__init__.py`、`docs/INVARIANTS.md`  
> **目标**：为 1.9.0 Mirasim 接入锁定 L1/L2/L3 的可行性、降级矩阵、权限边界与实现要点

---

## 0. 本机只读实测证据（摘要）

| 观测点 | 事实 | 影响 |
|--------|------|------|
| `~/.mirasim/setting.json` | 存在；含 `auth`(token/refresh/userId/exp)、`models`/`providers`/`profiles` 等（本 spike 未读取/输出密钥值，仅确认结构存在） | 合并器必须**严禁**触碰 auth/凭据/模型供应商密钥字段（INV-08） |
| `~/.mirasim/mcp.json` | **不存在** | L1 需定义“创建即合并”与幂等语义；目标文件可能为 `mcp.json` 或等价声明位置，需以 Mirasim 实际加载路径为准 |
| `~/.mirasim/mcp/` | 存在 `codex.toml` | 可能存在按宿主/agent 分片的 MCP 配置；合并策略需避免破坏分片文件 |
| `~/.mirasim/schedules/` | 存在 `setters.json`：`{"format":"mirasim.schedule-setters.v1","setters":{},"seedBefore":1794757039022}` | L3 可从 schedules 声明/运行态桥接显式 ingest/bootstrap 事件；当前无活跃 setter |
| `~/.mirasim/run/` | 目录存在 | 可作为运行态/会话归因线索（需只读探测） |
| `~/.mirasim/skills/mirasim-guide/SKILL.md` | 确认 Mirasim 为“把 Claude Code、Codex 等第三方 agent CLI 装进同一工作台的本地多 agent 编排器” | L2 hook 归属宿主 agent 侧，需按托管对象探测 |
| `~/.claude/settings.json` | 存在 `mcpServers` 与 `hooks`：`Stop`/`SessionEnd` → `~/.claude/hooks/mimir_auto_capture.py`（`timeout:60, async:true`） | Claude Code 路径 **L2 可探测且已具备自动捕获钩子形态**（本机证据） |
| `~/.codex/config.toml` | 存在 `[mcp_servers.*]` 多项；未见 hooks 等价块 | Codex 路径当前更接近 **L1(MCP)+显式/外层触发**；L2 需按 Codex 实际能力再探测，不可假定 |
| `~/.mirasim/bin/mirasim` | 本机不可用 | `doctor`/`ui-cli` 实机校验待有二进制环境补充 |

---

## L1：`mcp.json` 幂等合并 / 回滚 / doctor

### L1-1. 目标与约束

- **目标**：在不破坏既有配置的前提下，为 Mirasim 托管/关联环境声明 Mímir MCP server，使 `mimir_*` 工具可被发现与调用。
- **幂等**：同一输入重复执行，结果文件语义不变（仅允许时间戳字段在备份名中变化，主文件稳定）。
- **最小写入面**：仅合并 `servers[]`（或等价结构）中 Mímir 相关条目；**不得修改** `models`、`providers`、`profiles`、凭据、`auth*`、`mirachannel*`、密钥字段。
- **安全**：遵循 INV-08；dry-run 输出 diff 不含密钥值。

### L1-2. 数据模型（建议）

```json
{
  "servers": [
    {
      "name": "mimir",
      "enabled": true,
      "type": "stdio",
      "command": "python",
      "args": ["-m", "mimir_v8.mcp"],
      "env": {},
      "catalogId": "local.mimir",
      "agents": ["*"]
    }
  ]
}
```

- **合并键**：`name`（主）+ 可选 `catalogId`；冲突时默认“保留非 Mímir 用户自定义项，仅更新/插入目标 Mímir 条目”。
- **未知字段**：保留（forward-compatible）。

### L1-3. 合并算法（原型逻辑）

1. **Read**：若目标文件不存在，视为空对象 `{"servers": []}`。
2. **Validate**：解析 JSON；若结构非法则中止并报错（不覆盖）。
3. **Plan (dry-run)**：计算期望 `servers` 列表 = `existing_non_target + desired_mimir_entry`（按 name 去重、稳定排序可选）。
4. **Backup**：写前复制为 `<path>.bak-<YYYYmmddHHMMSS>`（同目录，权限保持）。
5. **Atomic write**：写临时文件 → `fsync` → `rename` 覆盖目标。
6. **Doctor**：校验 JSON 可解析、`name` 唯一、目标条目存在且 `enabled/type/command/args` 满足最小契约；可选探测 `command` 可执行性（不执行 MCP 协议握手作为硬失败，除非显式 `--strict`）。

### L1-4. 回滚

- **自动/手动**：`--rollback [--backup <path>]` 将最近备份原子恢复；无备份则拒绝并提示。
- **失败回滚**：若原子写失败，确保原文件未损坏（rename 失败则保留 tmp 并告警）。

### L1-5. Doctor 检查项（最小集）

| 检查 | 通过条件 | 失败级别 |
|------|----------|----------|
| 文件可解析 | JSON valid | error |
| servers 结构 | 数组或可归一化 | error |
| name 唯一 | 无重复 | error |
| Mímir 条目 | 存在且 enabled/command 合法 | warn/error（按策略） |
| 禁区字段未变更 | 与备份 diff 不含 auth/models/providers/profiles/凭据键 | error（INV-08） |

### L1-6. 原型（参考实现骨架，非生产接线）

```python
# docs/plans/spikes reference: pure merge + plan (no side effects on user dirs)
from __future__ import annotations
import json
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone

TARGET_NAME = "mimir"

def desired_entry() -> dict:
    return {
        "name": TARGET_NAME,
        "enabled": True,
        "type": "stdio",
        "command": "python",
        "args": ["-m", "mimir_v8.mcp"],
        "env": {},
        "catalogId": "local.mimir",
        "agents": ["*"],
    }

def merge_servers(doc: dict, entry: dict | None = None) -> dict:
    doc = deepcopy(doc) if isinstance(doc, dict) else {}
    servers = doc.get("servers")
    if not isinstance(servers, list):
        servers = []
    entry = entry or desired_entry()
    kept = [s for s in servers if not (isinstance(s, dict) and s.get("name") == entry["name"])]
    kept.append(entry)
    # stable-ish: keep relative order of others, target last
    doc["servers"] = kept
    return doc

def plan_merge(path: Path, entry: dict | None = None) -> tuple[dict, dict, str]:
    before = json.loads(path.read_text()) if path.exists() else {"servers": []}
    after = merge_servers(before, entry)
    diff_hint = "noop" if before == after else "update"
    return before, after, diff_hint

# write path (for real impl): backup -> write tmp -> fsync -> rename; rollback from newest bak
```

### L1-7. 结论与风险

- **可行性**：高（纯声明式 JSON 合并，边界清晰）。
- **主要风险**：Mirasim 实际加载路径/Schema 可能与假设不同（需在有 `mirasim` 二进制/`ui-cli` 的环境实测）；`~/.mirasim/mcp/codex.toml` 等分片配置的优先级需确认。
- **门禁**：实现前必须补“目标路径探测”与“密钥字段 diff 守卫”测试。

---

## L2：Hook 可探测性 / 降级矩阵

### L2-1. 目标与约束

- **目标**：在 Mirasim 托管多 agent 场景下，尽可能复用宿主 agent 的原生生命周期钩子完成会话摄入；无法探测/不可用时显式降级。
- **归因**：所有摄入必须携带 `host=mirasim`（当经 Mirasim 触发时）+ `agent`（claude/codex/…）+ `workspace`/`session`/`schedule` 等 `session_scope`，防串库（INV-04）。
- **不变量**：捕获失败/未启用不得伪装成功（INV-02）；自动产物默认入候选（INV-05）。

### L2-2. 可探测性证据与推断

| 宿主/Agent | 探测面 | 本机证据 | 可探测性 |
|------------|--------|----------|----------|
| Claude Code | `~/.claude/settings.json` → `hooks.Stop/SessionEnd` | 已配置 `mimir_auto_capture.py` | **高**（可读配置判定） |
| Codex | `~/.codex/config.toml` / 运行时 hooks 能力 | 仅见 MCP 配置，未见 hooks 块 | **中低**（需版本/能力探测；不可假定） |
| Mirasim 本体 | `ui-cli`/doctor/`~/.mirasim/run` | 二进制不可用，仅目录存在 | **待实机** |

### L2-3. 能力矩阵与降级路径

| 级别 | 条件 | 行为 | verdict/可观测性 |
|------|------|------|------------------|
| **A（原生钩子）** | 宿主 agent 暴露且可配置 SessionStart/End(或等价) | 通过 hook 调用 bridge/ingest；自动携带 session 元数据 | 正常 found/ingested；失败记 degraded + reason |
| **B（MCP 显式）** | 无钩子但 MCP 可用 | 依赖模型/流程显式调用 `mimir_ingest_conversation`/`mimir_remember` | 能力降级但可用；需提示“非自动捕获” |
| **C（外部触发）** | 无钩子无 MCP 自动链路 | 定时/手动脚本、schedules 桥接（L3）或 SessionEnd 外部封装 | 明确为 scheduled/manual；不得宣称实时自动 |

**降级原则**：A→B→C 单向降级并记录原因；禁止静默从 A 掉到“无记录”。

### L2-4. 归因字段（最小契约扩展建议）

在 envelope/trace 中统一携带（与 Adapter Contract 对齐）：
- `host`: `mirasim` | `claude-code` | `codex` | ...
- `agent`: 托管/实际执行 agent 标识
- `session_scope`: `workspace:<id>` / `session:<id>` / `schedule:<id>` / `remote-channel:<id>`
- `capture_mode`: `hook` | `mcp` | `explicit` | `schedule`

### L2-5. 结论与待实测

- **结论**：Claude 侧 L2 具备本机落地证据；Mirasim 作为编排层的统一 hook 注入需实机验证“透传 agent 钩子”边界。
- **待实测**：Mirasim 运行态下 `ui-cli` 能否暴露 agent/hook 状态；Codex 在目标版本的生命周期钩子能力；跨 agent 并发会话的 `session_scope` 唯一性。

---

## L3：Schedules 桥接触发点 / 权限边界

### L3-1. 目标与约束

- **目标**：将 Mirasim `schedules`/Goal 生命周期转为**显式** bootstrap/ingest 事件，支持 Thor/Odin 等节奏化运行的可追溯摄入。
- **非目标**：不隐式读取凭据、不绕过 ACL、不在无授权上下文下注入记忆。

### L3-2. 触发点设计

| 触发源 | 事件 | 映射动作 | 幂等键建议 |
|--------|------|----------|------------|
| schedule fire（setters/run） | `schedule.triggered` | `mimir_bootstrap`（按需）→ `mimir_ingest_conversation` 或结构化 event 候选 | `schedule:<id>:<planned_ts>` |
| schedule success/fail | `schedule.completed` | 记录执行结果候选/观测事件 | `schedule:<id>:<run_id>:result` |
| goal lifecycle（若可观测） | `goal.*` | 同上，scope=`goal:<id>` | `goal:<id>:<event_ts>` |

**证据**：本机 `setters.json` 当前为空，仅证明配置面存在；运行态触发需结合 `~/.mirasim/run/` 或 `ui-cli`/日志实测。

### L3-3. 权限边界

| 边界 | 规则 |
|------|------|
| 身份 | schedule 事件必须绑定 `owner_principal`（配置声明或映射表），不可匿名落库 |
| Scope | `session_scope=schedule:<id>` 或 `workspace:<id>`；跨 scope 查询/写入按 ACL 拒绝 |
| 最小读取 | 桥接器仅读取 schedule 元数据（id、时间、目标 agent/workspace 标识），**不读取** auth/token/密钥 |
| 写入策略 | 默认产出候选/观测事件（INV-05）；高风险自动晋升需显式开关+ADR |
| 审计 | 每次触发记录来源、幂等键、verdict（含 degraded） |

### L3-4. 失败与降级

- schedule 元数据不可读 → `degraded` + 不伪造事件。
- Mímir API 不可达 → 本地排队/重试策略需幂等（同 idempotency_key）；超出窗口显式失败。
- 重复触发 → 幂等去重（INV-03）。

### L3-5. 结论与待实测

- **可行性**：中高（配置与语义清晰），但依赖 Mirasim 暴露稳定的 schedule/run 标识。
- **待实测**：`setters.json` 与运行态 run 记录的关联方式；MiraChannel/Goal 事件总线是否可订阅；多 profile/多 workspace 下的 principal 映射。

---

## P2 汇总输出（验收对照）

| 交付项 | 状态 | 产物/位置 |
|--------|------|-----------|
| L1 合并逻辑原型 + 回滚 + doctor 设计 | 完成（Spike 级） | 本文 L1-3~L1-6 |
| L2 hook 探测方案 + A/B/C 降级矩阵 | 完成（Spike 级） | 本文 L2-2~L2-4 |
| L3 schedules 桥接触发点 + 权限边界 | 完成（Spike 级） | 本文 L3-2~L3-4 |
| 实机 `ui-cli`/二进制验证 | 待环境 | 需 `~/.mirasim/bin/mirasim` 可用后补充 |
| 生产代码实现与测试 | 未开始 | 进入 1.9.0 实现阶段（依 P1 契约与本 spike） |

---

## 对 P1 的回填建议

1. **BENCHMARK-COMPARE.md §4**：补充“Mirasim 自动触发对照”实测方案（A/B/C 路径各一组回放）。
2. **Adapter Contract**：在实现 Mirasim adapter 时补充 `mirasim.yaml` 实例与 `session_scope`/`capture_mode` 字段约定。
3. **INVARIANTS 门禁**：新增“配置合并不触碰凭据字段”的自动化检查样例（L1 实现时纳入 CI）。