# Issue 草案索引（2026-11-17 敌对审计）
> **状态（2026-11-25）**：02、04、05、09、10、12、14 已在 1.9.2 修复（对应 GitHub issue #65–#74），各文件顶部注明了出处。01、03、06、07、08、11、13 仍待处理。

> 用途：GitHub 登录已恢复，按下方顺序逐条提交。每个文件 = 一个 issue。
> **01 / 02 不要开公开 issue**——按 `SECURITY.md` 走 GitHub Security Advisory。
> 其余 12 条可以公开提交。`11` 是元问题，建议最后提，并把 03–10 的编号挂进它的 checklist。

| # | 标题 | 标签 | 严重度 |
|---|---|---|---|
| 01 | 联邦信封用发送方密钥加密：任一 peer 可解密他投并冒名发信 | security, federation | High（私有） |
| 02 | Vault 发布未拒绝符号链接，可写出库外 | security, vault | High（私有） |
| 03 | ACL 共享读不看 `visibility`，`confidential` 也不拦 | security, acl | Medium |
| 04 | `core_memory` 不在 `KNOWN_SCOPES`，该 scope 无法签发 | bug, auth | Medium |
| 05 | v7 `/delete` 只要 `write`，绕过 `delete` 分权 | security, auth | Medium |
| 06 | 可撤销最后一个 admin；注册表读改写有并发丢更新 | bug, auth, ops | Medium |
| 07 | `/stats`、`/bootstrap` 等端点 ACL 检查是 N+1 次新建连接 | performance | Medium |
| 08 | 卡片改名后被误判为 `human_content_changed`，发布永久停摆 | bug, vault | Low |
| 09 | Docker 镜像默认集合名 `mimir_v12_prod` 过不了自己的校验，启动即失败 | bug, deploy | High |
| 10 | MEX 导入直写 `facts`，绕过候选评审；列名来自载荷可注入 | security, governance | Medium |
| 11 | 版本遗迹总账：模块名、路由前缀、环境变量、集合名、测试名 | tech-debt, api | Meta |
| 12 | `learning.forget`/`correct` 不认别名，主 Agent 对 mentor 事实拿到 500 | bug, auth, governance | Medium |
| 13 | Dashboard 用 admin token 打所有写操作，密码即全权管理员 | security, dashboard, design | Medium |
| 14 | 出厂默认值带维护者机器路径、已退役主体与死版本号 | bug, config, onboarding | Medium |

提交顺序建议：09 → 04 → 05 → 06 → 03 → 07 → 10 → 08 → 11；01/02 走私密通道，不排进公开序列。

---

## 09 与 11 的关系

09 是 11 的实证：它之所以会让镜像起不来，正是因为 Dockerfile 里留着一个早已不存在的
`v12` 集合名，而校验器只认 `v8/v9/v10` 前缀。**这说明"历史命名"不只是观感问题，
而是会让部署直接坏掉。**
