# aduMEI 对标报告（v20.3 vs Mímir v14.1.0）· 2026-09-08

> 作者：Mímir KC 线（kimi:4ca97745）。应主线 `2026-09-08-mimir-doc-locations-and-daily-report.md` §四要求沉淀入仓。
> 前序：8/16《MIMIR_评估与对标报告》（v19.3.0 时代）、9/1 memmy 对标。本次针对 aduMEI 三周爆发期（v19.3.1→v20.3，10 个 release，commits 活跃至 2026-09-07）。

## 一、对象

**aduMEI**（github.com/monkey2jack/aiduMEI，MIT，GitHub-only 分发）：基于 mem0 的「智能体通用智慧引擎」。Python 3.10–3.12 + FastAPI + 嵌入式 Qdrant + SQLite/FTS5(trigram)，零构建控制台，MCP 41 工具，单租户自托管（官方自承「按租户收窄可见性，非硬隔离」）。测试 1743 用例（51% 覆盖，10 轮外部审计），资源实测 280–430MB。

## 二、三周演变（v19.3.1 → v20.3）

- **v19.4.0（8/17，分水岭）**：CHANGELOG 原文致谢「网友 Sandro 的 Mímir v12.0 白皮书」并落地六项——B1 治理管线（写后审计 + provisional 降权 0.30 + 规则/LLM 双评估器 + 5 态候选）、B2 backup_gate、B3 tombstone 可恢复遗忘、B4 召回侧注入框架、B5 轻量事件溯源账本、B6 opinion 信念层（≥2 来源防回声室）。同版加 Verbatim Vault 原文保真层（trigram FTS，融合进 /search）。
- **v19.4.1–19.5.0**：鉴权贯通、中文 trigram 根治（32.8ms→0.05ms）、幂等键、发布脱敏七面扫描闸门（负向对照焊死）。
- **v20.0（8/24）**：`(user_id, bank_id)` 二维作用域契约贯通全部读写；`benchmarks/` 可复现评测协议；容器去 root（暴露分 9.6→1.7）。
- **v20.1（8/26）**：确定性兜底 + 诚实召回——七类硬事实规则抽取层、三档整合降级（llm→extractive→rule）、`/search` 三态判语 `recall_verdict ∈ {found, not_found, degraded}`。
- **v20.2**：双引擎自动挡——本地 ONNX 备胎嵌入（bge-small-zh）+ 双索引 + 熔断半开探测 + 断供期 LLM 蒸馏欠账恢复后自动重放；挡位诚实上报；两轮生产断供演练。
- **v20.3（9/7，当前）**：一行 Prompt 全自动部署（canonical install prompt + 11 步验收）、e2e_smoke、restore_gate、systemd 内存限额；已有社区 PR（#10/#11）。

## 三、逐项对标

| 维度 | aduMEI v20.3 | Mímir v14.1.0 | 胜 |
|---|---|---|---|
| 写入治理 | 写后审计 + provisional 降权（写先于审） | 写前人审门禁 + 事件溯源 + 12 态状态机 | **Mímir**（差距缩小） |
| 审计/事件溯源 | 轻量事件账本（单表 8 类） | 全量事件溯源 + fact_versions + outbox 四投影 | **Mímir** |
| 注入防护 | 三层 guard + 写入门 + **服务端出口也包框架** | ≥31 模式 + 54 对抗用例 + 采集面 L1 | **aduMEI**（略先） |
| ACL/多租户 | tenant_clause + (user,bank) 作用域，非硬隔离 | per-agent ACL fail-closed + 演练验证 | **Mímir** |
| 生命周期 | 三轨衰减 + **tombstone 一键恢复** + 原文保真 | retention + tombstone + Ebbinghaus；原文豁免清理但不进召回 | **aduMEI** |
| 认知闭环 | Reflect 6h + 自编辑 + 人格基座 | reflect/crystallize + AutoSOP WikiSkill L0→L3 | 互有 |
| 韧性/降级 | **双引擎自动挡 + 欠账重放 + 挡位诚实**（市面独一份） | P46 通道三态断路器 + P48 LLM 失败语义化 | **aduMEI** |
| 召回 | 混合检索 + rerank + 原文融合 + 三态判语 | 三通道 RRF + ACL/decay 调权 + **金标实测 hit@10=1.000** | 互有（Mímir 有量化证据） |
| 联邦 | Pantheon 多 Agent 共享（单机内） | **跨节点 CRDT + Fernet 加密联邦** | **Mímir**（独代差） |
| 独有方向 | 控制台 RECALL/EVOLVE、IDE 钩子、社区化 | TKG 时态图谱、共享黑板、主动唤醒、跨模型投影 | **Mímir** |
| 资源 | 280–430MB | ~1.3G（bge-m3 常驻） | **aduMEI** |
| 测试工程 | 1743 用例 + 10 轮外审 + 发布脱敏闸门 | 444 全绿 + 金标 eval 哨兵 | **aduMEI** |
| 部署/分发 | 一行 Prompt 部署 + 公开仓 + 社区 PR | 私有；RC 门禁/备份链强 | **aduMEI** |

**总判**：8/16 时 Mímir 三条护城河（治理/审计/ACL），如今被 v19.4.0 追平大半。Mímir 剩余独代差四组：**联邦 CRDT 加密同步、TKG/黑板/唤醒/跨模型投影、写前人审+全量事件溯源、Eval 金标量化体系**。aduMEI 新建领先面：韧性自动挡、诚实召回判语、原文保真融合、测试/发布工程、部署可得性。对方抄作业速度以周计。

## 四、可借鉴清单（按优先级）

1. **P0 召回诚实判语**：`/search` 顶层 `recall_verdict ∈ {found, not_found, degraded}`——P46 通道三态遥测已有，只差用户面，成本极低，与「空≠错」纪律同源。
2. **P0 LLM 断供欠账重放**：规则兜底直写 + 恢复后重放补算（P48 顺势延伸；aduMEI v20.1/v20.2 已验证该形态）。
3. **P1 tombstone 可恢复遗忘**：物理删前全文快照 + `restore_tombstone` 一键回插并重建索引。
4. **P1 原文证据融合进召回**：原文层配额 `max(2, limit//4)`、主干优先、重合打标——原文保真已做，只差召回侧融合。
5. **P1 服务端注入出口包裹**：inject-context 类出口统一包「数据而非指令」框架（读侧仅剩缝隙）。
6. **P2 Tahoe-Gate 相关性闸门**：闲聊不触发检索，与 v13 主动唤醒互补。
7. **P2 发布脱敏闸门**：七面扫描 + 负向对照 + 词表外置——开源前置必做（v10 硬编码 key 事故有教训）。
8. **P2 一行 Prompt 部署 + report.py 机器可读自检**：init.sh 升级为 Agent 可执行正典 + 逐步退出码/JSON 证据。
9. **战略**：护城河继续向它短期抄不动的方向加深——联邦 CRDT、TKG 时态推理、Eval 金标体系、写前人审的不可替代性叙事。

## 五、来源

- GitHub [aiduMEI Releases v19.3.3–v20.3 全文](https://github.com/monkey2jack/aiduMEI/releases)、[VERSION-LINEAGE.md](https://raw.githubusercontent.com/monkey2jack/aiduMEI/main/docs/VERSION-LINEAGE.md)、README（commits 活跃至 9/7；API 限流 403，star 数未取，8/16 为 10）
- 本机 `MIMIR_评估与对标报告_2026-08-16.md`、`mimir_memmy_benchmark_20260901.md`、`aidumei_readme.md`（v19.3.0 快照）
- Mímir 侧 `CHANGELOG.md` v13.0–v14.1.0+post 节
