# Mímir v1.6.0 第二步：声明式探针与信源调度引擎实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

在 Mímir v1.6.0 中实现**声明式探针与信源调度引擎**：构建 `sources.yaml` 登记册，开发 GitHub Release 监听器，实现免费模型端点轻量探针，并落实断供欠账重放机制。这是对 v1.5.0 bootstrap API 隔离模型的能力扩展，使系统具备“主动抓取外部世界”的触角。

## Architecture

本计划分为三个子任务：

1. **信源登记册** - `sources.yaml` 配置文件和加载器
2. **模型探针引擎** - 定期探测免费模型池的轻量级探针
3. **断供欠账重放** - API 故障时的排队暂存与恢复机制

**关键文件**：
- `docs/superpowers/specs/2026-10-26-probes-sources-engineering-design.md` - 设计文档（待创建）
- `mimir_v8/store.py` - 新增探针相关方法（扩展现有）
- `mimir_v8/api.py` - 新增探针 API 端点
- `tests/test_probes_sources_engineering.py` - 测试套件（待创建）
- `docs/superpowers/plans/2026-10-26-probes-sources-engineering-plan.md` - 本实施计划

## Tech Stack

- YAML for sources configuration
- HTTP probing for model availability
- SQLite for queue management (existing facts table extension)
- Cron or background worker pattern for scheduled tasks
- Existing CanonicalStore infrastructure

## Spec Reference

- `docs/superpowers/specs/2026-10-26-probes-sources-engineering-design.md` - 完整设计文档（将在 Task 1 创建）
- SCHEMA_VERSION: 22 (继承自 Step 1)

## Global Constraints

- 必须与现有的 `sources.yaml` 格式兼容（如有）
- 探针必须是轻量级的（最小化对模型提供方的影响）
- 断供重放必须尊重现有的 principal isolation 逻辑
- 探针结果写入 `entities` 表和 `fact_entities` 关联
- 探针状态通过 `status` 字段（active/suspended）管理

---

# 实施任务分解

## ▍第一阶段：信源登记册

### Task 1: 创建 sources.yaml 配置文件和加载器

**Files:**
- Create: `mimir_v8/sources.yaml` - 新增配置文件
- Modify: `mimir_v8/store.py` - 新增 sources 加载方法

**Interfaces:**
- Consumes: 无（创建新文件）
- Produces: `sources.yaml` 配置文件内容

**步骤:**

- [ ] **步骤 1: 创建 sources.yaml** - 在 mimir_v8/ 下创建配置文件，包含：
  ```yaml
  # Mímir v1.6.0 Sources Configuration
  # Declaration of external information sources to monitor
  
  models:
    - name: "Nemotron-3-Ultra"
      kind: "free_model_pool"
      endpoint: "https://api.example.com/v1/models"
      description: "NVIDIA的免费模型池入口"
      tags: ["free", "ultra"]
    
    - name: "Llama-3.1-8B"
      kind: "free_model_pool"
      endpoint: "https://api.example.com/v1/models/llama"
      description: "Meta的开放模型"
      tags: ["free", "llama"]
  
  github:
    - repo: "litellm/litellm"
      kind: "release_monitor"
      description: "LiteLLM releases monitoring"
      monitor: "releases"
      filter: "prerelease=False"
  
  tools:
    - name: "sing-box"
      kind: "tool_catalog"
      endpoint: "https://api.example.com/v1/tools"
      description: "Sing-box tool catalog"
  ```

- [ ] **步骤 2: 编写 YAML 加载器** - 在 store.py 中实现：
  - `load_sources_yaml(path)` - 加载并验证 sources.yaml
  - 返回结构化的 sources 配置对象
  - 支持默认路径和自定义路径

- [ ] **步骤 3: 运行加载器测试** - 验证 YAML 解析和结构验证

- [ ] **步骤 4: 提交**
```bash
git add mimir_v8/sources.yaml mimir_v8/store.py
git commit -m "feat: add sources.yaml configuration and loader for v1.6.0"
```

**验收标准:** sources.yaml 创建成功，加载器能正确解析并返回结构化配置


### Task 2: 实现信源探针调度

**Files:**
- Modify: `mimir_v8/store.py` - 新增探针调度方法
- Modify: `mimir_v8/api.py` - 可选：添加探针 API 端点

**Interfaces:**
- Consumes: sources 配置，探针目标 URL
- Produces: 探针结果（存入 entities 表）

**步骤:**

- [ ] **步骤 1: 编写探针调度器** - 在 store.py 中实现：
  - `probe_model_endpoint(endpoint, timeout=30)` - 对模型端点发送轻量 HTTP GET
  - 返回：`{status: "ok"|"unreachable", latency_ms, error?}`
  - 必须在超时前返回，不得阻塞

- [ ] **步骤 2: 编写探针结果写入** - 将探针结果持久化：
  - 存入 `entities` 表（model 类型）
  - 通过 `fact_entities` 关联到相关 facts
  - 记录 `sensitivity="internal"`，`visibility="all"`

- [ ] **步骤 3: 添加 API 端点** - 可选：
  - `GET /v16/probe/status` - 返回当前探针状态
  - 鉴权：需要 write 权限

- [ ] **步骤 3: 运行集成测试** - `pytest tests/ -v` 关注探针相关

- [ ] **步骤 4: 提交**
```bash
git add mimir_v8/store.py mimir_v8/api.py
git commit -m "feat: add probe scheduler and API for v1.6.0"
```

**验收标准:** 探针能成功探测模型端点，结果正确持久化到 DB


### Task 3: 实现断供欠账重放机制

**Files:**
- Modify: `mimir_v8/store.py` - 新增队列管理方法
- Modify: `mimir_v8/api.py` - 新增重放 API 端点

**Interfaces:**
- Consumes: 排队的探针任务，恢复信号
- Produces: 重放后的探针结果

**步骤:**

- [ ] **步骤 1: 编写队列管理方法** - 在 store.py 中实现：
  - `queue_probe_task(entity_id, endpoint, result_snapshot)` - 排队探针任务
  - `get_due_probe_tasks(now)` - 获取到期的探针任务
  - `complete_probe_task(task_id, result)` - 标记任务完成

- [ ] **步骤 2: 编写重放 API** - 在 api.py 中实现：
  - `POST /v16/probe/replay` - 手动触发排队任务的重放
  - 鉴权：需要 admin 或 write 权限
  - 返回：重放结果摘要

- [ ] **步骤 3: 测试排队与重放** - 验证任务可进入/离开队列

- [ ] **步骤 3: 提交**
```bash
git add mimir_v8/store.py mimir_v8/api.py
git commit -m "feat: add debt replay mechanism for v1.6.0"
```

**验收标准:** 排队任务进出正常，重放 API 能成功触发挂起的探针


## ▍第二阶段：测试与验收

### Task 4: 编写探针与重放测试

**Files:**
- Create: `tests/test_probes_sources_engineering.py`

**步骤:**

- [ ] **步骤 1: 编写测试** - tests/test_probes_sources_engineering.py 包含：
  - test_load_sources_yaml_basic
  - test_probe_model_endpoint_ok
  - test_probe_model_endpoint_timeout
  - test_queue_probe_task
  - test_complete_probe_task

- [ ] **步骤 2: 运行测试** - `pytest tests/test_probes_sources_engineering.py -v`

- [ ] **步骤 3: 提交**
```bash
git add tests/test_probes_sources_engineering.py
git commit -m "test: add probe and engineering tests for v1.6.0"
```

**验收标准:** 所有探针与重放测试通过


### Task 5: 向后兼容性验证

**Files:**
- Test: 现有测试回归 (`tests/test_p15d_bootstrap_api.py` 等)

**步骤:**

- [ ] **步骤 1: 运行回归测试** - 验证 v1.5.0 行为不受影响
- [ ] **步骤 2: 提交**
```bash
git add tests/test_p15d_bootstrap_api.py
git commit -m "test: verify backward compatibility after v1.6.0 Step 2"
```

**验收标准:** 所有原有测试仍然通过


---

# 执行路径选择

**两种执行方式任选其一:**

## 选项 A: Subagent-Driven (推荐)

我调度独立 subagent 逐任务执行，任务之间暂停审阅进度，确保每个任务独立正确后再进入下一任务。

**操作:** 请告诉我 "使用 subagent-driven-development"，我将调度子代理。

## 选项 B: Inline Execution (本 session)

我在当前 session 中逐任务执行，每个任务完成后检查结果，必要时纠错。

**操作:** 请告诉我 "使用 inline execution"，我将开始执行第一个任务。

---

## 里程碑

完成所有任务后，验收标准：
- [x] sources.yaml 配置文件创建并可加载
- [x] 探针调度器能探测模型端点并返回结果
- [x] 探针结果持久化到 entities/fact_entities 表
- [x] 断供欠账重放队列正常工作
- [x] 重放 API 能触发挂起的任务
- [x] 所有探针与重放测试通过
- [x] 所有原有测试仍然通过 (v1.5.0 回归)
- [x] 文档已更新 (design spec + implementation plan)

**完成后:** 进入 v1.6.0 Step 3：早报生成与双层结晶流水线

---

## 实施记录（2026-10-26）

- **Task 1 信源登记册**：`sources.yaml`（models/github/tools）+ `load_sources_yaml`
  加载器（含默认路径与防御性校验），`tests/test_sources_loader.py` 5 项通过。
- **Task 2 模型探针调度**：`probe_model_endpoint`（轻量 GET，超时归一为
  `unreachable`）+ `record_model_probe`（写入 `model_probe` 实体，ok→active /
  unreachable→suspended，关联 `probe_target` fact）+ `GET /v16/probe/status`。
- **Task 3 断供欠账重放**：新增 `probe_tasks` 表，`queue_probe_task` /
  `get_due_probe_tasks` / `complete_probe_task` + `POST /v16/probe/replay`。
- **附带修复**：Step 1 遗留的 `SCHEMA_VERSION` 不一致
  （迁移写 22 但常量仍 21，迁移库二次启动会校验失败）→ 常量升 22，全新库
  `SCHEMA_SQL` 直接纳入实体三表与 `probe_tasks`。

测试：`tests/test_probes_sources_engineering.py`（7）、`tests/test_probes_api.py`（4）、
原实体 21 项、sources 5 项、bootstrap 回归全部通过。
