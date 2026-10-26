# Mímir v1.6.0 实体知识库 - 第一步设计文档

## 概述

本文档描述 **Mímir v1.6.0** 的第一步：构建实体知识库的基础表结构。这是一个加法式迁移，从 **SCHEMA_VERSION 21** 升级到 **SCHEMA_VERSION 22**。

**目标**：在现有事实存储之上补齐三张核心表，支持：
- **entities**：结构化实体（模型、工具、免费模型池条目等）的持久化
- **fact_entities**：事实与实体的多对多桥接（哪条 fact 对应哪些实体）
- **entity_relations**：实体间的关系图谱（依赖、引用、聚类）

**非目标**：实体探针引擎、每日早报合成、模型免费池自动探活（这些将在后续的第二步/第三步实施）。

---

## 1. 变更概览

| 版本 | 变更 | 类型 |
|------|------|------|
| 21 → 22 | 新增 `entities` 表 | 加法式（无表删除/修改） |
| 21 → 22 | 新增 `fact_entities` 表 | 加法式 |
| 21 → 22 | 新增 `entity_relations` 表 | 加法式 |
| 21 → 22 | 更新 `schema_meta` 中的 `schema_version` 为 22 | 系统元数据 |

---

## 2. 实体表结构

### 2.1 `entities` 表

存储结构化实体的定义，每条记录代表一个可被查询、引用和关联的知识条目。

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `entity_id` | TEXT | PRIMARY KEY | 实体唯一标识（UUID 或惯例：`{domain}_{name}_{hash}`） |
| `entity_type` | TEXT | NOT NULL | 实体类型：`model`, `tool`, `source`, `concept`, `agent` 等 |
| `name` | TEXT | NOT NULL | 实体可读名称 |
| `domain` | TEXT | NOT NULL | 归属域：`free_model_pool`, `tool_catalog`, `ai_model`, `knowledge_base` 等 |
| `description` | TEXT | | 实体描述 |
| `metadata_json` | TEXT | | JSON 格式的扩展元数据（版本、端点、成本、上下文窗口等） |
| `sensitivity` | TEXT | default "internal" | 敏感度级别：`internal`, `restricted`, `confidential` |
| `visibility` | TEXT | default "all" | 可见性：`all`, `owner_only`, `private` |
| `status` | TEXT | default "active" | 实体状态：`active`, `deprecated`, `suspended` |
| `valid_from` | TEXT | | ISO-8601 开始时间戳（双时态有效期起点） |
| `valid_to` | TEXT | | ISO-8601 结束时间戳（双时态有效期终点，NULL 表示永久有效） |
| `created_at` | TEXT | NOT NULL | 创建时间戳 |
| `updated_at` | TEXT | NOT NULL | 最后更新时间戳 |
| `content_hash` | TEXT | | 内容哈希，用于变更检测和去重 |

**索引**：
- `idx_entities_domain` (`domain`)
- `idx_entities_type` (`entity_type`)
- `idx_entities_status` (`status`)
- `idx_entities_valid` (`valid_from`, `valid_to`) -- 用于双时态查询

---

### 2.2 `fact_entities` 表

将 `facts` 表中的事实与 `entities` 表中的实体建立多对多关联。记录“哪条 fact 关联了哪些实体”，支持事实被多个实体引用，也一条 fact 关联多个实体。

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `fact_id` | TEXT | NOT NULL, FK → facts.fact_id | 被关联的事实 |
| `entity_id` | TEXT | NOT NULL, FK → entities.entity_id | 关联的实体 |
| `role` | TEXT | | 在该实体语境下事实的角色（`subject`, `object`, `modifier`, `context` 等） |
| `confidence` | REAL | default 1.0 | 关联置信度 |
| `created_at` | TEXT | NOT NULL | 关联创建时间 |

**唯一约束**：`UNIQUE(fact_id, entity_id)` — 同一事实同一实体不应有重复关联。

**索引**：
- `idx_fact_entities_fact` (`fact_id`)
- `idx_fact_entities_entity` (`entity_id`)
- `idx_fact_entities_role` (`role`)

---

### 2.3 `entity_relations` 表

记录实体间的二元关系，构建知识图谱。支持传递性推理和路径查询。

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `from_entity_id` | TEXT | NOT NULL, FK → entities.entity_id | 源实体 |
| `to_entity_id` | TEXT | NOT NULL, FK → entities.entity_id | 目标实体 |
| `relation_type` | TEXT | NOT NULL | 关系类型：`depends_on`, `calls`, `references`, `part_of`, `clusters_with`, `subsumes` 等 |
| `relation_direction` | TEXT | default "forward" | 方向：`forward`, `backward`, `bidirectional` |
| `strength` | REAL | default 1.0 | 关系强度（0.0-1.0，用于聚类权重、路径优先级） |
| `metadata_json` | TEXT | | 关系扩展元数据（上下文、证据、条件等） |
| `valid_from` | TEXT | | 关系开始生效时间 |
| `valid_to` | TEXT | | 关系失效时间 |
| `created_at` | TEXT | NOT NULL | 关系创建时间 |

**唯一约束**（可选，视图而定）：
`UNIQUE(from_entity_id, to_entity_id, relation_type)` — 同一源目标对同一关系类型不应有重复定义。

**索引**：
- `idx_entity_relations_from` (`from_entity_id`)
- `idx_entity_relations_to` (`to_entity_id`)
- `idx_entity_relations_type` (`relation_type`)
- `idx_entity_relations_bidir` (`from_entity_id`, `to_entity_id`) -- 用于双向查询

---

## 3. 双时态有效期设计

所有三张表均包含 `valid_from` / `valid_to` 列，实现**双时态**支持：

- **事实有效期**：反映实体/关系在不同时间段的有效状态
- **时序失效（非删除）**：当模型下线或工具废弃时，旧关系的 `valid_to` 被设置为失效时间，而非物理删除记录
- **查询语义**：`WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)` 筛选当前有效的实体/关系

**默认行为**：
- 新增记录默认 `valid_from = now`，`valid_to = NULL`（永久有效）
- 迁移时现有记录保持 `valid_from = NULL, valid_to = NULL`，向后兼容

---

## 4. 迁移脚本

### 4.1 SQL 迁移

```sql
-- =============================================
-- Mímir v1.6.0: 实体知识库基础表迁移 (v21 → v22)
-- =============================================

-- 1. 更新 schema_version
UPDATE schema_meta SET value = '22' WHERE key = 'schema_version';

-- 2. 创建 entities 表
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT,
    metadata_json TEXT,
    sensitivity TEXT DEFAULT 'internal',
    visibility TEXT DEFAULT 'all',
    status TEXT DEFAULT 'active',
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT
);

-- 索引：entities 表
CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities.entity_type;
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities.status;
CREATE INDEX IF NOT EXISTS idx_entities_valid ON entities(valid_from, valid_to);

-- 3. 创建 fact_entities 表
CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fact_id, entity_id),
    FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);

-- 索引：fact_entities 表
CREATE INDEX IF NOT EXISTS idx_fact_entities_fact ON fact_entities(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_fact_entities_role ON fact_entities(role);

-- 4. 创建 entity_relations 表
CREATE TABLE IF NOT EXISTS entity_relations (
    from_entity_id TEXT NOT NULL,
    to_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    relation_direction TEXT DEFAULT 'forward',
    strength REAL DEFAULT 1.0,
    metadata_json TEXT,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_entity_id, to_entity_id, relation_type),
    FOREIGN KEY (from_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);

-- 索引：entity_relations 表
CREATE INDEX IF NOT EXISTS idx_entity_relations_from ON entity_relations(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_to ON entity_relations(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_type ON entity_relations(relation_type);

-- =============================================
-- 迁移完成
-- =============================================
```

### 4.2 Python 迁移函数

在 `mimir_v8/store.py` 中添加迁移入口（在现有的 `upgrade_schema` 或类似模式下调用）：

```python
def migrate_schema_21_to_22(conn) -> None:
    """从 SCHEMA_VERSION 21 迁移到 22：实体知识库基础表。
    
    该迁移只添加新表，不修改或删除现有表/数据。
    """
    from contextlib import closing
    
    # 更新元数据
    conn.execute(
        "UPDATE schema_meta SET value = '22' WHERE key = 'schema_version'"
    )
    
    # 创建 entities 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            domain TEXT NOT NULL,
            description TEXT,
            metadata_json TEXT,
            sensitivity TEXT DEFAULT 'internal',
            visibility TEXT DEFAULT 'all',
            status TEXT DEFAULT 'active',
            valid_from TEXT,
            valid_to TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            content_hash TEXT
        )
    """)
    
    # 创建 fact_entities 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_entities (
            fact_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            role TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (fact_id, entity_id),
            FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        )
    """)
    
    # 创建 entity_relations 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_relations (
            from_entity_id TEXT NOT NULL,
            to_entity_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            relation_direction TEXT DEFAULT 'forward',
            strength REAL DEFAULT 1.0,
            metadata_json TEXT,
            valid_from TEXT,
            valid_to TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (from_entity_id, to_entity_id, relation_type),
            FOREIGN KEY (from_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
            FOREIGN KEY (to_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        )
    """)
    
    # 创建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_valid ON entities(valid_from, valid_to)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_fact ON fact_entities(fact_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_role ON fact_entities(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_from ON entity_relations(from_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_to ON entity_relations(to_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_type ON entity_relations(relation_type)")
```

---

## 5. 基础 CRUD API 端点（v1.6.0 初版）

### 5.1 `POST /v16/entities` - 创建实体

```python
@app.post("/v16/entities", status_code=201)
def create_entity(
    body: dict,
    identity: Principal = Depends(scoped("write")),
) -> dict:
    """创建结构化实体。
    
    用于登记免费模型池条目、工具目录条目、AI 模型定义等。
    权限：写入权限（通常为 admin 或系统自动）。
    """
    from .schema import CreateEntity  # 需要在 schema.py 中定义
    
    # 鉴权：仅 admin 可创建实体（或系统自动创建）
    if not identity.is_admin:
        raise AuthError("entity creation requires admin", 403, "admin_required")
    
    # 验证必填字段
    entity_type = body.get("entity_type")
    name = body.get("name")
    domain = body.get("domain")
    if not all([entity_type, name, domain]):
        raise HTTPException(422, "entity_type, name and domain are required")
    
    # 生成 entity_id（惯例：domain_name_uuid，或使用提供的 ID）
    entity_id = body.get("entity_id") or f"{domain}_{name.lower().replace(' ', '_')}_{uuid4().hex[:8]}"
    
    # 插入 entities 表
    conn = context.store.connect()
    try:
        conn.execute(
            """INSERT INTO entities 
               (entity_id, entity_type, name, domain, description, metadata_json,
                sensitivity, visibility, status, created_at, updated_at, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_id,
                entity_type,
                name,
                domain,
                body.get("description"),
                json.dumps(body.get("metadata", {})),
                body.get("sensitivity", "internal"),
                body.get("visibility", "all"),
                body.get("status", "active"),
                utc_now(),
                utc_now(),
                body.get("content_hash"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    
    return {"entity_id": entity_id, "status": "ok"}
```

### 5.2 `GET /v16/entities/{entity_id}` - 获取实体

```python
@app.get("/v16/entities/{entity_id}")
def get_entity(
    entity_id: str,
    identity: Principal = Depends(scoped("read")),
) -> dict:
    """根据 ID 获取实体详情。"""
    conn = context.store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM entities WHERE entity_id=? AND status!=? AND (valid_to IS NULL OR valid_to > ?)",
            (entity_id, "suspended", utc_now()),
        ).fetchone()
        if not row:
            raise NotFoundError(entity_id)
        return dict(row)
    finally:
        conn.close()
```

### 5.3 `POST /v16/fact-entities` - 关联事实与实体

```python
@app.post("/v16/fact-entities", status_code=201)
def link_fact_entity(
    body: dict,
    identity: Principal = Depends(scoped("write")),
) -> dict:
    """将事实与实体建立关联。
    
    例如：将一条“模型评测”事实关联到对应的 model 实体上，
    以便后续检索时按实体过滤。
    """
    # 鉴权...
    fact_id = body.get("fact_id")
    entity_id = body.get("entity_id")
    role = body.get("role", "context")
    
    conn = context.store.connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO fact_entities (fact_id, entity_id, role, confidence, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fact_id, entity_id, role, 1.0, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
    
    return {"status": "ok", "fact_id": fact_id, "entity_id": entity_id}
```

### 5.4 `GET /v16/entities/search` - 按实体检索

```python
@app.get("/v16/entities/search")
def search_entities(
    domain: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    identity: Principal = Depends(scoped("read")),
) -> dict:
    """按条件检索实体。"""
    conn = context.store.connect()
    try:
        query = "SELECT * FROM entities WHERE 1=1"
        params = []
        
        if domain:
            query += " AND domain=?"; params.append(domain)
        if entity_type:
            query += " AND entity_type=?"; params.append(entity_type)
        if status:
            query += " AND status=?"; params.append(status)
        
        # 应用双时态过滤
        query += " AND (valid_to IS NULL OR valid_to > ?)"
        params.append(utc_now())
        
        rows = conn.execute(query, params).fetchall()
        return {"entities": [dict(r) for r in rows]}
    finally:
        conn.close()
```

### 5.5 `GET /v16/fact-entities/{fact_id}` - 获取事实关联的实体

```python
@app.get("/v16/fact-entities/{fact_id}")
def get_fact_entities(
    fact_id: str,
    identity: Principal = Depends(scoped("read")),
) -> dict:
    """获取事实关联的所有实体。"""
    conn = context.store.connect()
    try:
        rows = conn.execute(
            """SELECT e.*, fe.role, fe.confidence
               FROM entities e
               JOIN fact_entities fe ON e.entity_id = fe.entity_id
               WHERE fe.fact_id = ? AND e.status != 'suspended'
               AND (e.valid_to IS NULL OR e.valid_to > ?)""",
            (fact_id, utc_now()),
        ).fetchall()
        return {"fact_id": fact_id, "entities": [dict(r) for r in rows]}
    finally:
        conn.close()
```

### 5.6 `GET /v16/entity-relations/{from_entity_id}` - 获取实体关系

```python
@app.get("/v16/entity-relations/{from_entity_id}")
def get_entity_relations(
    from_entity_id: str,
    relation_type: str | None = Query(default=None),
    identity: Principal = Depends(scoped("read")),
) -> dict:
    """获取指定实体的出边关系。"""
    conn = context.store.connect()
    try:
        query = """SELECT e2.*, er.relation_type, er.relation_direction, er.strength
                   FROM entities e1
                   JOIN entity_relations er ON e1.entity_id = er.from_entity_id
                   JOIN entities e2 ON er.to_entity_id = e2.entity_id
                   WHERE e1.entity_id = ? AND e1.status != 'suspended'
                   AND (e2.valid_to IS NULL OR e2.valid_to > ?)"""
        params = [from_entity_id, utc_now()]
        
        if relation_type:
            query += " AND er.relation_type=?"; params.append(relation_type)
        
        rows = conn.execute(query, params).fetchall()
        return {
            "from_entity_id": from_entity_id,
            "relations": [dict(r) for r in rows]
        }
    finally:
        conn.close()
```

---

## 6. 向后兼容性与升级路径

1. **增量迁移**：本次迁移仅添加新表，不修改现有 `facts` 等表结构，现有数据保持不变。
2. **向后兼容**：SCHEMA_VERSION 升级到 22 后，老版本的客户端/实例仍可正常运行（仅略过新表的操作）。
3. **双时态向后兼容**：现有记录 `valid_from/valid_to` 为 NULL，查询时默认全部视为有效，逐步过渡到双时态模式。
4. **回滚方案**：若需要回滚，直接删除新表并将 `schema_meta` 重置为 `'21'`，无需迁移数据。

---

## 7. 测试计划

### 7.1 迁移测试

```python
def test_migrate_schema_21_to_22():
    """验证 SCHEMA_VERSION 21 → 22 的迁移脚本正确执行。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        
        # 先确保 SCHEMA_VERSION 21 环境
        conn = store.connect()
        
        # 运行迁移
        migrate_schema_21_to_22(conn)
        
        # 验证新表存在
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in cursor.fetchall()]
        
        assert "entities" in table_names, "entities table not created"
        assert "fact_entities" in table_names, "fact_entities table not created"
        assert "entity_relations" in table_names, "entity_relations table not created"
        
        # 验证 schema_version 更新
        cursor.execute("SELECT * FROM schema_meta WHERE key='schema_version'")
        row = cursor.fetchone()
        assert row["value"] == "22", f"schema_version should be 22, got {row['value']}"
        
        # 验证表结构
        for table in ["entities", "fact_entities", "entity_relations"]:
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [c[1] for c in cursor.fetchall()]
            assert len(cols) > 0, f"{table} should have columns"
        
        conn.close()
```

### 7.2 CRUD 测试

```python
def test_crud_entities():
    """测试实体的增删改查。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        conn = store.connect()
        
        # 确保迁移已运行
        migrate_schema_21_to_22(conn)
        
        # 1. 创建实体
        entity_id = store.create_entity({
            "entity_type": "model",
            "name": "Nemotron-3-Ultra",
            "domain": "free_model_pool",
            "description": "NVIDIA的免费模型",
            "metadata": {"context_window": 128_000, "cost_per_token": 0},
        })
        
        # 2. 查询实体
        entity = store.get_entity(entity_id)
        assert entity["name"] == "Nemotron-3-Ultra"
        assert entity["domain"] == "free_model_pool"
        
        # 3. 更新实体
        store.update_entity(entity_id, status="deprecated")
        entity = store.get_entity(entity_id)
        assert entity["status"] == "deprecated"
        
        # 4. 删除实体（软删除：设置 status=deleted 或 tombstone）
        # 此处演示软删除
        
        conn.close()
```

### 7.3 关联测试

```python
def test_fact_entity_linking():
    """测试事实-实体关联。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        conn = store.connect()
        migrate_schema_21_to_22(conn)
        
        # 创建事实
        fact_id = store.create_fact({
            "content": "Nemotron-3-Ultra 在 MIMIR 中的评测结果",
            "domain": "knowledge",
            "fact_type": "reference",
            "visibility": "all",
            "sensitivity": "internal",
        }, actor_principal="system")
        
        # 关联实体
        store.link_fact_entity(fact_id, entity_id="model_Nemotron-3-Ultra", role="subject")
        
        # 查询关联
        entities = store.get_fact_entities(fact_id)
        assert len(entities["entities"]) == 1
        assert entities["entities"][0]["name"] == "Nemotron-3-Ultra"
        
        conn.close()
```

---

## 8. 完成标准

**当完成以下任务时，本步骤算作完成**：

1. ✅ `migrate_schema_21_to_22()` 函数在 `mimir_v8/store.py` 中实现
2. ✅ 三张新表 (`entities`, `fact_entities`, `entity_relations`) 创建成功
3. ✅ 对应的 SQLite 索引创建成功
4. ✅ `schema_meta` 版本更新为 `"22"`
5. ✅ 基础 CRUD API 端点在 `mimir_v8/api.py` 中实现（`/v16/entities`, `/v16/fact-entities`, `/v16/entity-relations/search` 等）
6. ✅ 相应的测试用例在 `tests/` 下编写并通过（至少迁移测试 + 基础 CRUD 测试）
7. ✅ 向后兼容性验证：老版本实例在 v22 schema 下正常运行（略过新表）

**下一步**：完成本设计文档后，使用 `writing-plans` 把任务拆解为具体的代码实现子任务，按 TDD 逐步实施。

---