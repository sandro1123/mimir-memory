# `/stats`、`/bootstrap` 等端点的 ACL 检查是 N+1 次新建连接

**标签**：performance, api
**严重度**：Medium
**影响版本**：1.5.0 起

## 问题

`store.can_access` 每被调用一次就 `self.connect()` 开一个新的 SQLite 连接：

```python
with contextlib.closing(self.connect()) as connection:
    fact = connection.execute("SELECT ... FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
```

而下列端点都是"全表取出 → 逐条 `can_read`"：

- `/stats`（api.py 798–809 `visible_stats`）
- `/v8/bootstrap`（api.py 949–953）与 `/bootstrap`
- `/v8/memories/recent`（api.py 1329–1334）
- `/v13/wake` 的锚定通道（`relevance.py` 里同样逐条判定）

生产量级（万级事实）意味着单次请求数千次 `sqlite3.connect()` 打开/关闭：延迟以秒计，
文件描述符抖动，且这是一个只读端点，任何 `read` 权限 token 都能触发。

`/bootstrap` 尤其致命——它是每个 Agent **每次会话启动**都会打的第一个请求。

## 修复

1. `visible_stats` / `memories_recent` / `bootstrap` 改成**一次连接 + 一次查询**：把可见性判定下推为 SQL（`owner_principal = ? OR domain IN (...) AND fact_type IN (...) AND sensitivity != 'restricted' OR EXISTS(grant)`），或在 Python 里批量预取 `resource_grants` 后本地判定。
2. `can_access` 保持原样（单条路径语义清晰），但新增 `visible_fact_ids(principal)` 供列表类端点使用；两者共享同一套判定函数，避免第二份真相。
3. 给 `/bootstrap` 加一个"每 principal 缓存 30s"的选项（它返回的世界观变化很慢）。

## 验收（可证伪）

- 在 2 万事实的夹具上，`/stats` 的 SQLite 连接建立次数从 O(N) 降到 O(1)（可用 mock 计数）。
- `/bootstrap` P95 延迟与事实总量脱钩（10 条与 2 万条两档对照，差异 < 2x）。
- 权限语义不变：现有 ACL 测试全绿；外加一条"不可见事实数=0 时结果与旧实现逐条一致"的对照测试。
