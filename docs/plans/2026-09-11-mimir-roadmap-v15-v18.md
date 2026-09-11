# Mímir 长远路线图 · 信任与深耕 (v15 → v18)

> 2026-09-11 用户 GO（全按建议）。定位裁决：**个人/小团队的可信记忆底座**——
> 治理+账本+ACL+诚实遥测打成招牌，向架构深耕走；不卷 mem0 式产品化渠道打法。
> 六家对标一手数据（2026-09-11 实测）：mem0 65.1k★ / Cognee 30.6k★ /
> ai-memory 6.5k★ / aduMEI v20.5（联邦授权+密码学谱系）/ Zep（时态实体图）/
> Memobase（结构化画像）。九差距清单已勘定，本图即对策。

## v15 通血（进行中，~09-20）
四线不变（docs/plans/2026-09-11-mimir-v15-plan.md）：喂料✅收官 /
联邦三段式 / 检索闭环四件 / 仪表盘 v5 止血。
**插队件**：Claude Code skill（自吃狗粮，一天）。

## v16 信任与互通（~10 月上旬）
1. **LOCOMO + LongMemEval 跑分**（差距#1，最先——数字出来之前一切架构
   投资缺背书）：接入两公开基准 + 自建金标 8→24（#35 已裁）；
   发布方法论+数字到 README（学 mem0 开源评估框架可复现）。
2. **MEX 记忆交换格式**（差距#2）：facts+版本+事件+来源 JSON 导入导出
   （mimir-migrate export/import）+ COGX→Mímir 导入器（借 Cognee 的
   四家适配器格局反向受益）。
3. **谱系哈希链**（差距#6）：fact_versions 加 previous_version_hash
   链式咬合（tamper-evident 措辞学 aduMEI 诚实披露）；verify 端点补
   链尾对账（他们审计的元教训「验证端点绿灯制造虚假信心」——照抄
   修好的终态设计，少踩他们踩过的坑）。
4. **联邦授权 Grants**（差距#5）：grantor/grantee/resource_scope/
   动作谓词/有效期/撤销——与 v15 联邦实测同挂 peer 信任面。

## v17 实体与画像（~10 月下旬，最大投入 ~3 天+）
5. **实体链接+消解**（差距#4 核心）：抽取链产出 entities 表 +
   facts↔entities 关联（mem0 检索增强路线）；消解后置 v17.5。
6. **TKG 通电**：实体进 relations 图（写路径已有）；Zep 式过期
   作废不删除——Chronos+valid_from/until 底子已在，喂料开闸后
   流量会来。
7. **结构化画像**（差距#8）：Memobase 式 topic/sub_topic 之上做
   **只读投影**（从 facts 聚合 user_pref/iron_rule，不新增真相源）；
   GET /v12/profile 底子已在。
8. **引擎挡位一等化**（差距#7）：MIMIR_ENGINE_GEAR=cloud/auto/local，
   local=关键词+确定性抽取+ONNX 本地嵌入（aduMEI 同思路）。

## v18 生态（11 月）
9. **Harness 家族**（按用户密度）：Claude Code skill → MCP 打磨 →
   Cursor/Zed（MCP-only 便宜）→ Codex。
10. **任务交接协议**（差距#9）：POST /v12/handoff 任务上下文包
    （目标/已试路径/开放问题/相关事实引用）→ MEX 子格式。
11. **控制台 v2**：联邦 tab + 实体图可视化 + 谱系链浏览器。

## v19+（方向题，立而不排）
多用户团队化 · 强不可抵赖谱系（签名/WORM）· 本体定义 · 情感衰减轨。

## 用户拍板记录（2026-09-11）
定位=可信底座+深耕 ✅ · 跑分先于 MEX ✅ · v17 实体图进主干 ✅ ·
Claude Code skill 插队 ✅（全按建议）。
