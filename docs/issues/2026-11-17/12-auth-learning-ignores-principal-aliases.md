# `learning.forget` / `correct` 不认别名：主 Agent 对 mentor 的事实会拿到 500

> **状态（2026-11-25）**：已修复，见 #68（1.9.2）。


**标签**：bug, auth, governance
**严重度**：Medium
**影响版本**：1.5.0 起（别名继承落地时漏了学习管线）

## 问题

别名继承在 `auth.Principal.can_act_as` 与 `store.can_access` 里都实现了，所以
`heimdallr` 能读、能通过路由层的权限检查。但 `mimir_v8/learning.py` 里是裸字符串比较：

```python
def forget(self, fact_id, ...):
    fact = self.store.get_fact(fact_id)
    if fact["owner_principal"] != actor_principal:          # 225 行区域
        raise PermissionError("only the fact owner can forget a fact")

def correct(self, fact_id, ...):
    ...
    if fact["owner_principal"] != actor_principal:
        raise PermissionError("only the fact owner can correct a fact")
```

API 路由先过了 `require_fact_permission(identity, fact, "delete"/"write")`
（api.py 1595 / 1603），这一步会放行别名；随后 `learning.forget` 又用严格相等否掉。

## 影响

1. **功能断**：`heimdallr` 无法 forget / correct 那 469 条 `mentor` 事实——恰恰是
   "平滑退役 mentor、主 Agent 无感接管"承诺里最需要能改的那批。
2. **错误码骗人**：`PermissionError` 没有被 api 的异常处理器覆盖
   （只处理 `ValueError` / `ConflictError` / `NotFoundError` / `CandidatePolicyError`），
   最终落到通用 `Exception` 处理器，返回 **500 internal_error**，而不是 403。
   调用方会以为是服务故障，运维会去查日志而不是权限。
3. 同类裸比较还可能存在于其它服务里（`opinion.py`、`autoskill.py`、`core_memory._authorize`
   用的是 `actor != agent_id`），需要一次性清点。

## 修复

1. `learning` 里改用与路由相同的一套判定：`Principal.can_act_as` 或
   `store.can_access(..., permission)`，不要在服务层另立第二份身份规则。
2. 顺带把 `PermissionError` 映射成 403（`owner_boundary`），别让它变成 500。
3. 给出唯一入口：建议在 `auth.py` 里加 `is_owner(actor, owner_principal)` 并在所有
   服务层调用，避免下一次再漏。

## 验收

- `heimdallr` 对 `mentor` 的事实调 `/v8/learning/correct` → 201（进候选），不是 500。
- `quantmaster` 对 `mentor` 的事实同调用 → 403 `owner_boundary`。
- `tests/test_p15a_principal_aliases.py` 扩这条服务层用例。
