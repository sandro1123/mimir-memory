# Mímir v1.6.0 实体知识库实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

在 Mímir v1.6.0 中构建实体知识库的基础设施：补齐 `entities`、`fact_entities`、`entity_relations` 三张核心表，并提供对应的 CRUD API、迁移脚本以及测试用例。这是对 v1.5.0 bootstrap API 隔离模型的基础设施加固，支持结构化实体的持久化、事实-实体关联和实体关系图谱。

## Architecture

本计划遵循**加法式迁移**原则：只在 SCHEMA_VERSION 21（当前）的基础上新增表，无修改现有表结构。设计包含三个核心模块：

1. **实体存储 (`entities`)** - 结构化实体（模型、工具、概念等）的持久化，支持双时态有效期
2. **事实-实体关联 (`fact_entities`)** - 多对多桥接表，记录哪条 fact 对应哪些实体
3. **实体关系图谱 (`entity_relations`)** - 二元关系表，构建实体间的依赖、引用、聚类网络

所有表均支持 `valid_from`/`valid_to` 双时态，实现“时序失效而非物理删除”，向后兼容原有数据。

**关键文件**：
- `mimir_v8/store.py` - 新增 migrate_schema_21_to_22()、CRUD 方法
- `docs/superpowers/specs/2026-10-26-entities-knowledge-base-design.md` - 设计文档（已创建）
- `tests/test_entities_migration.py` - 迁移与 CRUD 测试（待创建）
- `docs/superpowers/plans/2026-10-26-entities-knowledge-base-plan.md` - 本实施计划

## Tech Stack

- Python 3.10+
- SQLite (CANONICALE_STORE)
- FastAPI (已存在的 API 框架)
- pytest (测试框架)
- JSON for metadata storage

## Spec Reference

- `docs/superpowers/specs/2026-10-26-entities-knowledge-base-design.md` - 完整设计文档
- SCHEMA_VERSION: 21 → 22

## Global Constraints

- schema_meta 版本从 "21" 更新为 "22" 仅在迁移时更新，老版本实例正常运行
- 仅添加新表，不修改现有 facts 等表结构
- 所有新增字段在旧实例中通过默认值（NULL）静默忽略
- 双时态查询默认：valid_to IS NULL OR valid_to > now
- FK 约束使用 ON DELETE CASCADE，但实际删除由应用层控制（软删除）

---

# 实施任务分解

## ▍第一阶段：迁移脚本与基础表

### Task 1: 实现 migrate_schema_21_to_22() 迁移函数

**Files:**
- Modify: `mimir_v8/store.py:870-970` (新增 migrate_schema_21_to_22 方法)

**Interfaces:**
- Consumes: 无
- Produces: 数据库模式迁移 (3 张新表 + 7 个索引)

**步骤:**

- [ ] **步骤 1: 编写迁移函数** - 在 store.py 中实现 migrate_schema_21_to_22()，包含：
  - 更新 schema_meta 版本为 "22"
  - 创建 entities 表 (entity_id PK, entity_type, name, domain, description, metadata_json, sensitivity, visibility, status, valid_from, valid_to, created_at, updated_at, content_hash)
  - 创建 fact_entities 表 (fact_id + entity_id PK, role, confidence, created_at, FK 外键)
  - 创建 entity_relations 表 (from_entity_id + to_entity_id + relation_type PK, relation_direction, strength, metadata_json, valid_from, valid_to, created_at, FK 外键)
  - 创建所有必要的 SQLite 索引

- [ ] **步骤 2: 运行迁移测试** - 验证新表创建成功，schema_version 更新正确

- [ ] **步骤 3: 提交**
```bash
git add mimir_v8/store.py
git commit -m "feat: add migrate_schema_21_to_22 for v1.6.0 entity knowledge base"
```

**验收标准:** 运行 `python -c "from mimir_v8.store import CanonicalStore; s = CanonicalStore(':memory:'); s.migrate_schema_21_to_22(); assert 'entities' in [t[0] for t in s.connect().execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]`


### Task 2: 验证迁移正确性

**Files:**
- Create: `tests/test_entities_migration.py`

**Interfaces:**
- Consumes: migrate_schema_21_to_22() 返回值
- Produces: 验证通过/失败的测试结果

**步骤:**

- [ ] **步骤 1: 编写测试** - tests/test_entities_migration.py 包含：
  - 测试迁移前后 schema_version 从 21 变为 22
  - 测试 3 张新表 (entities, fact_entities, entity_relations) 创建成功
  - 测试关键列存在 (如 entity_id, domain, sensitivity 等)
  - 测试索引创建情况

- [ ] **步骤 2: 运行测试验证失败** - `pytest tests/test_entities_migration.py -v` 应该显示初始设置失败 (因为 store 还未集成)

- [ ] **步骤 3: 迁移实现后重新运行** - 应该全部通过

- [ ] **步骤 3: 提交**
```bash
git add tests/test_entities_migration.py
git commit -m "test: add entity migration verification tests"
```

**验收标准:** 所有测试通过，确认迁移脚本健壮


## ▍第二阶段：实体 CRUD 操作

### Task 3: 实现实体 CRUD 方法

**Files:**
- Modify: `mimir_v8/store.py` (在 migrate_schema_21_to_22 之后新增)

**Interfaces:**
- Consumes: entity_type, name, domain, 可选: description, metadata, sensitivity, visibility, status, content_hash
- Produces: {"entity_id": str, "status": str}

**步骤:**

- [ ] **步骤 1: 编写 create_entity() 方法** - 创建实体并返回 entity_id
  - 参数: entity_type (必需), name (必需), domain (必需), description?, metadata?, sensitivity (default "internal"), visibility (default "all"), status (default "active"), content_hash?
  - 返回: {"entity_id": new_id, "status": status}
  - 需要处理 entity_id 生成 (UUID 或 domain_name_hash 惯例)

- [ ] **步骤 2: 编写 get_entity() 方法** - 根据 ID 获取实体 (双时态筛选)
  - 参数: entity_id (必需), 可选: now (当前时间戳)
  - 返回: dict|None (若已过期/被暂停返回 None)
  - 查询条件: entity_id = ? AND status != 'suspended' AND (valid_to IS NULL OR valid_to > now)

- [ ] **步骤 3: 编写 update_entity() 方法** - 更新实体属性
  - 参数: entity_id (必需), 可选的字段更新
  - 返回: bool (True = 有行被更新)
  - 仅更新传入的字段，其他保持不变

- [ ] **步骤 4: 编写 soft_delete_entity() 方法** - 软删除实体
  - 参数: entity_id (必需), 可选: now
  - 行为: 将 status 设为 'deleted' 并记录 valid_to = now
  - 返回: bool

- [ ] **步骤 5: 编写测试** - tests/test_entities_crud.py
  - test_create_entity_basic
  - test_get_entity_before_and_after_update
  - test_update_entity_partial
  - test_soft_delete_entity

- [ ] **步骤 5: 运行测试** - `pytest tests/test_entities_crud.py -v`

- [ ] **步骤 6: 提交**
```bash
git add mimir_v8/store.py tests/test_entities_crud.py
git commit -m "feat: add entity CRUD methods for v1.6.0"
```

**验收标准:** 所有 CRUD 测试通过，包括软删除后 get_entity 返回 None


### Task 4: 测试实体 CRUD 边界情况

**Files:**
- Modify: `tests/test_entities_crud.py` (追加测试)

**步骤:**

- [ ] **步骤 1: 添加边界测试** - 测试如下情况：
  - 更新不存在的实体 (返回 False)
  - 同时更新 description 和 status
  - 软删除后再次软删除 (应该是 idempotent 或返回 False)
  - get_entity 在 valid_to 已过期时返回 None

- [ ] **步骤 2: 运行测试** - `pytest tests/test_entities_crud.py -v`

- [ ] **步骤 3: 提交**
```bash
git add tests/test_entities_crud.py
git commit -m "test: add entity CRUD boundary tests"
```

---

## ▍第三阶段：事实-实体关联

### Task 5: 实现事实-实体关联方法

**Files:**
- Modify: `mimir_v8/store.py` (在 Task 3 之后)

**Interfaces:**
- Consumes: fact_id, entity_id, 可选: role, confidence, now
- Produces: bool (成功/失败)

**步骤:**

- [ ] **步骤 1: 编写 link_fact_entity() 方法** - 关联事实与实体
  - 参数: fact_id (必需), entity_id (必需), role (default None), confidence (default 1.0), now
  - 行为: INSERT OR REPLACE INTO fact_entities (UPSERT 语义)
  - 返回: bool

- [ ] **步骤 2: 编写 unlink_fact_entity() 方法** - 移除关联
  - 参数: fact_id (必需), entity_id (必需)
  - 行为: DELETE FROM fact_entities WHERE fact_id=? AND entity_id=?
  - 返回: bool (是否有行被删除)

- [ ] **步骤 3: 编写 get_fact_entities() 方法** - 获取事实关联的实体
  - 参数: fact_id (必需), 可选: now
  - 返回: list[dict] (每个 dict 包含 entity 信息 + fe.role + fe.confidence + fe.created_at)
  - 查询需 JOIN entities + fact_entities，双时态筛选

- [ ] **步骤 4: 添加测试** - tests/test_entities_fact_entity.py
  - test_link_fact_entity_basic
  - test_unlink_fact_entity
  - test_get_fact_entities_returns_correct

- [ ] **步骤 5: 运行测试** - `pytest tests/test_entities_fact_entity.py -v`

- [ ] **步骤 6: 提交**
```bash
git add mimir_v8/store.py tests/test_entities_fact_entity.py
git commit -m "feat: add fact-entity linking methods for v1.6.0"
```

**验收标准:** 所有测试通过，链接/解链/查询全部正确


### Task 6: 测试事实-实体关联边界情况

**Files:**
- Modify: `tests/test_entities_fact_entity.py` (追加)

**步骤:**

- [ ] **步骤 1: 添加边界测试** - 测试：
  - 关联不存在的事实 (应 gracefully handle)
  - 关联不存在的实体 (应 gracefully handle)
  - 同一 fact_entity 的重复链接 (UPSERT 行为)
  - 不同时间戳下的查询

- [ ] **步骤 2: 运行测试** - `pytest tests/test_entities_fact_entity.py -v`

- [ ] **步骤 3: 提交**
```bash
git add tests/test_entities_fact_entity.py
git commit -m "test: add fact-entity linking boundary tests"
```

---

## ▍第四阶段：实体关系图谱

### Task 7: 实现实体关系方法

**Files:**
- Modify: `mimir_v8/store.py` (在 Task 5 之后)

**Interfaces:**
- Consumes: from_entity_id, to_entity_id, relation_type, 可选: relation_direction, strength, metadata, now
- Produces: bool (成功/失败)

**步骤:**

- [ ] **步骤 1: 编写 create_entity_relation() 方法** - 创建实体间关系
  - 参数: from_entity_id (必需), to_entity_id (必需), relation_type (必需), 
    relation_direction (default "forward"), strength (default 1.0), metadata?
  - 行为: INSERT OR REPLACE INTO entity_relations (UPSERT)
  - 返回: bool

- [ ] **步骤 2: 编写 get_entity_relations() 方法** - 获取出边关系
  - 参数: from_entity_id (必需), 可选: relation_type, now
  - 返回: list[dict] (每个 dict: to_entity_id, to_name, relation_direction, strength, metadata_json, related_at)
  - 查询: FROM entity_relations JOIN entities ON to_entity_id

- [ ] **步骤 3: 编写 get_incoming_relations() 方法** - 获取入边关系
  - 参数: to_entity_id (必需), 可选: relation_type, now
  - 返回: list[dict] (每个 dict: from_entity_id, from_name, relation_direction, strength, metadata_json, related_at)
  - 查询: FROM entity_relations JOIN entities ON from_entity_id (反向查询)

- [ ] **步骤 4: 添加测试** - tests/test_entities_relations.py
  - test_create_entity_relation_basic
  - test_get_entity_relations_returns_correct
  - test_get_incoming_relations_returns_correct
  - test_relation_direction_forward_and_backward

- [ ] **步骤 5: 运行测试** - `pytest tests/test_entities_relations.py -v`

- [ ] **步骤 6: 提交**
```bash
git add mimir_v8/store.py tests/test_entities_relations.py
git commit -m "feat: add entity relation methods for v1.6.0"
```

**验收标准:** 所有测试通过，包括正向和反向关系查询


### Task 8: 测试实体关系边界与高级查询

**Files:**
- Modify: `tests/test_entities_relations.py` (追加)

**步骤:**

- [ ] **步骤 1: 添加边界测试** - 测试：
  - 查询不存在的实体 (返回空列表)
  - 查询已过期的关系 (valid_to 检查)
  - 不同 relation_type 的过滤
  - strength 排序验证

- [ ] **步骤 2: 运行测试** - `pytest tests/test_entities_relations.py -v`

- [ ] **步骤 3: 提交**
```bash
git add tests/test_entities_relations.py
git commit -m "test: add entity relation advanced tests"
```

---

## ▍第五阶段：向后兼容性验证

### Task 9: 验证向后兼容性

**Files:**
- Modify: 无 (主要在 store.py 迁移函数内)
- Test: `tests/test_backward_compatibility.py` (新建)

**步骤:**

- [ ] **步骤 1: 编写兼容性测试** - 验证 v1.5.0 行为在 v1.6.0 schema 下不受影响
  - test_bootstrap_still_works_after_migration
  - test_existing_facts_not_affected
  - test_old_schema_version_readable

- [ ] **步骤 2: 运行测试** - `pytest tests/test_backward_compatibility.py -v`

- [ ] **步骤 3: 提交**
```bash
git add tests/test_backward_compatibility.py
git commit -m "test: verify backward compatibility after v1.6.0 migration"
```

**验收标准:** bootstrap API 测试仍然全部通过，确认旧数据未被破坏


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
- [ ] migrate_schema_21_to_22() 运行成功，3 张新表存在
- [ ] 所有 CRUD 测试通过 (create, read, update, soft-delete)
- [ ] fact-entity linking 测试全部通过
- [ ] entity relations 测试全部通过 (正向 + 反向)
- [ ] bootstrap API 仍然完全通过 (v1.5.0 回归测试)
- [ ] 文档已更新 (design spec + implementation plan)

**完成后:** 进入 v1.6.0 Step 2：声明式探针与信源调度引擎