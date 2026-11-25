# ACL 共享读不看 `visibility`，`confidential` 也不拦

**标签**：security, acl
**严重度**：Medium
**影响版本**：1.5.0 起

## 问题

`mimir_v8/store.py` 的 `can_access`（2257–2282）：

```python
domain_sharable = (
    permission == "read"
    and fact["domain"] in SHARED_DOMAINS          # infrastructure / knowledge / tech_support
    and fact["fact_type"] in SHARED_TYPES         # reference / learning
    and fact["sensitivity"] != "restricted"
    and "authenticated" in roles
)
```

`visibility` 虽然从库里取出来了（2243），但**在共享读判定里完全没参与**。因此：

- `visibility='owner_only'` 的技术域 `reference` 事实，任何已认证 principal 都能读。
- `sensitivity='confidential'` 同样不拦（只拦 `restricted`）。

README 第 125 行承诺的是"personal 域与 restricted 保持严格隔离"——这两条确实成立，
但 `owner_only` / `confidential` 两个字段在技术域读侧是装饰性的，使用者会按字段名
做出错误预期。

另有一处死路径：`visibility='shared'` 在创建时授予角色 `federated_agents`
（`store.py` 1379–1383），但没有任何接口能把该角色发给 principal（`mint_principal`
只写调用方给的角色，文档里也没有约定），所以这条授权实际上永远不生效。

## 需要先回答的问题

这不是"改一行"的问题，先要定下意图：

- 若**域优先**（当前实现）：`visibility` 应改名为 `publication_visibility`，并在创建接口的文档里写清"技术域默认共享，owner_only 不阻止同机读取"。
- 若**字段优先**：`domain_sharable` 追加 `and fact["visibility"] != "owner_only" and fact["sensitivity"] == "internal"`。

## 建议

选字段优先（更符合直觉，也更安全），并补一条反向注入测试把语义钉死：
同一技术域 `reference` 事实，`visibility='owner_only'` 时 `can_read(other_principal)` 必须为 False。

同时清理 `federated_agents` 死角色：要么给它一条签发路径，要么删掉。
