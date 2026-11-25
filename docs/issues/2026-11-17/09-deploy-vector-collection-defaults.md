# Docker 镜像默认集合名 `mimir_v12_prod` 过不了自己的校验，启动即失败

> **状态（2026-11-25）**：已修复，见 #65（1.9.2）。


**标签**：bug, deploy
**严重度**：High（默认镜像不可用）
**影响版本**：1.7.0 镜像

## 问题

`mimir_v8/vector_projector.py` 的集合名白名单只有 v8/v9/v10 前缀：

```python
VECTOR_COLLECTION_PREFIXES = (
    "mimir_v8_shadow_", "mimir_v8_prod_",
    "mimir_v9_shadow_", "mimir_v9_prod_",
    "mimir_v10_shadow_", "mimir_v10_prod_",
)
```

`validate_vector_collection_name` 不在其中就抛 `VectorProjectionError`；
`runtime._build_vector`（125–129）把它包成 `RuntimeConfigurationError`，启动直接失败。

而部署侧的默认值三处不一致：

- `Dockerfile:35` → `MIMIR_V8_COLLECTION=mimir_v12_prod` ← **不在白名单，镜像默认起不来**
- `docker-compose.yml:19` → `mimir_v10_prod_local` ← 在白名单，compose 能起
- `mimir_v8/server.py:17,55` 与 `runtime.py:202` → `mimir_v8_prod_facts` ← 裸机默认

所以：`docker run <image>` 用镜像自带默认值 → 启动失败；`docker compose up` → 成功，
但落到**另一个**集合；裸机运行 → 又是第三个集合。三套部署把向量写进三个不同的
collection，检索侧看起来"记忆丢了"。

## 复现

```bash
docker build -t mimir:test .
docker run --rm mimir:test          # 未覆盖 MIMIR_V8_COLLECTION
# 期望：服务启动；实际：RuntimeConfigurationError / vector collection must use ...
```

## 修复

1. 统一默认集合名（建议 `mimir_prod_facts`，与 v8 解耦），并在 `runtime.py` 里做**唯一**默认值来源，`server.py`/Dockerfile/compose 全部引用它。
2. 白名单改为"允许 `mimir_(shadow|prod)_*` 的通用命名空间"，而不是枚举历史版本前缀；否则下次改名还会踩。
3. 启动失败信息里给出**当前值**与**合法示例**，现在是句笼统的提示。
4. 加一条 CI：解析 Dockerfile 的 ENV，断言每个集合默认值都通过 `validate_vector_collection_name`。

## 验收

- `docker run` 不覆盖任何变量即可 `/health` 正常。
- 裸机、compose、镜像三条路径落到同一个集合名（可用 `scripts/prod_drift.py` 加一项对拍）。
- 白名单新增名字不再需要改代码。

## 关联

这是 issue #11（版本遗迹）的直接后果：Dockerfile 里留着一个已经不存在的 `v12` 命名。
