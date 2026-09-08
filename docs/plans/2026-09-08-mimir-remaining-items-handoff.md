# Mímir 审计修复 · 未完成事项移交单 · 2026-09-08

- 收件：Mímir 记忆系统开发会话（名册登记 key `claude:74553bd8-09ec-4bfe-8892-60e2356936c0`；若该会话已换代，以 `list_sessions` 当前名册为准）
- 发件：审计/修复会话（`claude:5c0ecd1e-8673-4c38-a37d-fbc209614c72`）
- 依据：`分层讨论/Mímir-全面审计-2026-09-07.md`（§0 结论、§1~§7 取证、§8 处置顺序、§9 处置结果）；同文件也在本机 `<本机工作目录>\mimir_audit_20260907\`
- 本单自包含，不依赖发件会话的上下文。

---

## 1. 已完成，勿重做（现网状态快照）

| 项 | 状态 |
|---|---|
| 版本 | 生产 API **v14.1.0 / Schema 20**；发布树 `~/.hermes/mimir/releases/v14.1.0-20260907`（含热修 `mimir_v8/symbolic_memory.py`，备印 `.bak-P47-symbolic-20260908`）；venv `~/.hermes/mimir/venvs/v14.1.0-20260907`，`venvs/current` 指向它 |
| 代码 | 开源仓 master `82fd596`，tag `v14.1.0` = `86c1a54`，gitee/github 已推；关键提交 b9b0099 治理 / 6b2f529 error_hook / 54f9696 人审语义 / da72195 插件 / fb5def2 看板 / 5f24345 投影 / d494103 symbolic 自愈 |
| 单元 | 18 个 `mimir*.service` 已 sed 到新树；`mimir-v9.2-governance.service` 新增 `EnvironmentFile=~/.hermes/mimir/secrets/evaluator.env`；备份 `backups/units-pre-v14.1.0-20260907/` |
| 生产仓 `~/.hermes/mimir` | `138277d`（部署）+ `0c03e86`（ops health 三哨兵 + verify 算 disputed + 离机备份脚本）；`collect/rss_seen_urls.json` 已取消跟踪 |
| 数据修复 | 4 条 disputed 事实经 `mimir-migrate reproject --with-vector` 重放，fts/graph/vector 与 canonical 对齐，`verify ok=True`；146 条人批事实经 PATCH（`expected_version`）回填 `human_status=confirmed`（146 条 `fact.updated` 事件），active 事实 confirmed 21→166 |
| 备份 | Mímir 离机备份 `ops/mimir_offsite_backup.sh` + 用户级 `mimir-offsite-backup.timer` 每日 03:50；首跑成功 `hermes-offsite/mimir-critical-20260908-000035.tar.gz.gpg`（17MB）；标记 `~/.hermes/mimir/backups/.last-offsite-success` |
| 看板 | `~/mimir-dashboard/backend/main.py` 与 `frontend/index.html` 与仓库 md5 一致（4.1.0）；仓库目录已改为 `dashboard/backend/main.py` |
| 插件 | `~/.hermes/plugins/mimir_memory_provider/` 已是 14.1.0（per-agent token / `{ok,error}` / sha256 幂等 / reflect text），**但网关未重启，尚未生效** |
| 清理 | 归档目录：`backups/units-bak-archive-20260907/`、`backups/ops-bak-archive-20260907/`、`archive/decoys-20260907/`（mimir.db、data/canonical.db、core/fts5_index.db 三枚诱饵库）、`archive/tmp_sync-20260907/`、`~/mimir-dashboard/bak-archive-20260907/` |
| 回归 | 设备端 `pytest tests`：490 passed / 3 errors（仅 `TestM1dHermesPluginContract`，预存债） |

---

## 2. 未做事项（按建议优先级）

### 2.1 Hermes 四网关重启，让插件 14.1.0 生效（需用户择时，Heimdallr 网关有「勿动」先例）
- 对象：system `hermes-gateway.service`（Heimdallr）+ 用户级 `hermes-gateway-{jarvis,mentor,quantmaster}.service`。
- 验证：重启后 30 分钟内 `audit_log action='query'` 的 `actor_principal` 应出现 jarvis/mentor/quantmaster/heimdallr（此前 30 天 admin 1185 次、各 agent 2~3 次）；`mimir_remember` 失败时 agent 应看到 `{"ok": false, "error": ...}`。
- 风险：agent 首次以自己 token 查询，owner_only 事实不再跨 agent 可见（这是设计目标）；若召回质量明显下降，可用 `MIMIR_PLUGIN_TOKEN_FILE` 环境变量按网关单独回退。

### 2.2 抽取链 P1-5：LLM 失败 = 永久丢弃
- 现状：`worker.llm_extract_once` → `Evaluator._parse_response(None)` → `parse_error="LLM returned empty response"` → `decide()` → `discard` → `extraction_runs.status='cancelled'` 且 `ingestion_runs.status='extracted'`，该对话永不再进队列。9router 7 天重启 19 次（09-07 22:12~22:19 七分钟 12 次），抖动窗内对话全部静默丢弃；`cancelled` 同时承载正常 discard / exact_duplicate / LLM 故障，事后不可区分。
- 建议：`EvaluationResult` 加 `llm_unavailable` 标记；此时 `extraction_runs` 记 `failed`（`error_code='llm_unavailable'`），`ingestion_runs` 保持 `stored` 以便下轮重试，并加简单退避（同一 run 24h 内最多 N 次）；health 加「近 2h extraction failed 率」哨兵。历史被丢弃的对话无法回补（无标记）。

### 2.3 生产 `mimir_config.yaml` v7 死键清理
- 文件 `~/.hermes/mimir/mimir_config.yaml`（`version: 7.0.0`），代码只读 `federation.*` 与 `collector.*`；`agents/subscriptions/domains/fact_types/visibility/water_level/autodream/awareness/rerank/gatekeeper/decay/core_memory.storage_dir/persist_dir/fts5.db_path` 均无消费者（`fts5.db_path` 指向已归档的 `core/fts5_index.db`）。
- 做法：先 `cp` 备份到 `backups/`，只留 `version`、`federation`、`collector` 三段（模板见 `scripts/init.sh` 新版）；动手前 `grep -rn` 确认 `ops/` 与 Mentor cron 脚本无读；改后重启 `mimir.service` 验证 `federation.agents` 仍注册（`POST /v8/facts owner=quantstar` 201）。

### 2.4 `/v11/symbolic/offload` 校验缺口 + 冒烟残留
- 端点接受空 `raw_text`（09-08 00:0x 冒烟以 `{}` 成功写入一条空块：`node_id sym_10f7f8e6-49e`，`block_id ba33e716-be6c-4b9e-a470-c678e863a08c`，owner/session admin）。
- 建议：`raw_text` 非空校验（422）；删除该残留块与其 canvas（`symbolic_blocks` / `symbolic_canvases` 非事件溯源表，直接 DELETE 可接受，或补一个 delete 端点）。

### 2.5 看板三层「空≠错」语义化
- `_mimir_get/_mimir_post/_db_query` 现已记日志但仍返 None/[]；前端 `fetchJSON` 已有 `netErr` 角标，但各面板仍把空当正常。建议后端返回 `{"degraded": true, "error_sources": [...]}` 形态（`dash_*` 三端点已是此形态，可推广到 overview/facts/governance 等），前端在 devMode 面板显示。

### 2.6 监控后续
- 新哨兵阈值观察一周：治理成功率（近 2h 有 ≥3 次评估且 0 成功即报；上线后前 2h 会因旧失败行报一次「0/102」，自清）、`human_review` 积压 >40 或最老 >14d、离机备份标记 >48h。
- Mentor cron「Mímir v8 一致性核对」的 `cron_incidents` 三条 `alerted` 未 ack（09-05/06/07），09-08 03:00 应回绿，需 ack 清零。
- 飞书巡检「健康静默」设计的告警疲劳问题（P45 误报 3392 次 vs 真告警 7 天无人看）：建议加「连续 N 次同因聚合」。

### 2.7 P2 债（审计报告 §3 全清单，此处列需要开发动作的）
- `relations` 483 行全部 2026-08-01 迁移写入、之后零新增；TKG `valid_from/valid_until` 全空——图谱通道是静态遗物（路线图「七处减法①」）。
- 13 张零行表（documents×5 / learning×2 / code×2 / retention_jobs / knowledge_feedback_signals / governance_suggestions / fact_assets）+ `knowledge_items` 6 行测试残留。
- vault 457 篇只进 `conversation_sources`（#41-A 已通电 wiki 双路由，但抽取闸门仍只放行 `conversation`）。
- `search_feedback` 30 天 5 行；AutoSOP 生产零结晶；`skill_topics` 2 行为 09-03 验收残留。
- `tests/test_v12_insight.py::TestM1dHermesPluginContract` 3 errors 结构性（`sys.path` 指机器本地路径、import 不存在的 `provider.py`、断言 v12.0.0 的 hooks）——重写为 MemoryProvider ABC 契约或删除；CONTRIBUTING/CI 的 `pytest tests/` 因此永不全绿。
- CHANGELOG 8 个版本无 git tag（v13.0.0/v12.2.0/v12.0.x/v11/v10/v9.x）。
- 死代码：`mimir_v8/coalesce.py`（零引用）、`migration.py` 的 `migrate_schema_v14/v15/v19` 无入口、`mcp.py` 27 工具无 `[project.scripts]` 入口且无 per-tool 计数；`hermes-plugin/mimir_memory_provider.py` 单文件死副本。
- 56 个 `MIMIR_*` 环境变量代码在读、文档只写 14 个；名字漂移 `MIMIR_HERMES_STATE_DB` vs `MIMIR_CONNECTOR_HERMES_STATE_DB`，`MIMIR_EVAL_API` vs `MIMIR_EVAL_API_URL`。
- 文档矛盾残留：ARCHITECTURE「v13 schema 19 不变」vs 19→20、`valid_during`、`/v10/opinions/consolidate`（真路径 observations）、ROADMAP `?at=` vs `at_timestamp`；dashboard README 环境变量表与 `main.py` 真读键不符。

### 2.8 上游依赖（Hermes 线，请转告而非在 Mímir 修）
- 9router（user unit，`127.0.0.1:20128`）7 天 19 次 `Started`；Hermes 网关同窗 `APIConnectionError provider=9router`。Mímir 抽取与治理都挂在它上面。
- system `hermes-gateway.service`（Heimdallr）09-07 三次 `status=9/KILL`，lifecycle_ledger 报 `exited UNCLEANLY`，疑 OOM（drop-in `MemoryMax=4G`）。它是 Mímir 最大喂料源（hermes_cdc 1927 条）。

### 2.9 磁盘
- `venvs/v12.1.4-20260902`（4.8G）计划 09-10 后清；`venvs/v14.0.0-20260903`（4.8G）建议 v14.1.0 稳定一周后清。当前 `venvs/` 15G，盘 64%。

### 2.10 明早观察点（09-08）
- 03:00 一致性核对回绿（`verify ok`）；03:20 在线备份；03:50 离机备份第二跑（笔记本 `hermes-offsite/` 出现第二个 `mimir-critical-*`）。
- 首个新候选进入治理后 15 分钟内 `candidate_review_assessments` 出现 `success=1, model='cbcn/deepseek-v4-flash'`（09-07 22:0x 已用生产凭据直调验证 success/0.97，但真实候选流转尚未观察到——当时 18 条待审已被用户在看板全部批准）。
- `candidate.requeued` 事件不再增长（部署后至 00:05 为 0）。

---

## 3. 施工纪律（本轮新判例，接手必读）
1. **行尾**：仓库 74 个文件 CRLF、其余 LF，且工作树可能与 HEAD 风格漂移（governance.py 工作树 CRLF/HEAD LF）。补丁一律以 `git show HEAD:<path>` 的风格回写（本机 `<本机工作目录>\mimir_audit_20260907\patchlib.py` 的 `head_nl`）。改后 `git diff --numstat` 必看，整文件翻红即行尾污染。
2. **新旧库 schema 漂移已五例**（conversation_sources CHECK / graph_edges / candidate_review_assessments+governance_decisions / symbolic_* owner_principal）：任何 `CREATE TABLE IF NOT EXISTS` 扩展表都要在构造器里配守卫式 `ALTER` 自愈，且要有「老形态表」测试。
3. **施工中用户会并行操作生产**（09-07 23:23 在看板批完 18 条待审）：部署前后对拍 DB 差异先查 `review_actions` / `audit_log`，再怀疑代码。
4. 生产写入只走 API（事件溯源）或停机 CLI；`reproject` 含 vector 时必须停 `mimir.service`；回填类 PATCH 需 `expected_version`，且会触发向量重嵌入使 `/ready` 短暂 503（146 条约 2 分钟）。
5. 部署配方（五次先例）：`git archive` 新树 → `cp -a` 克隆 venv + sed editable finder → 18 单元 sed（先备份）→ ops 4 脚本 sed → `venvs/current` 软链 → 迁移/修复 → 重启 → `/ready` 等 bge-m3 加载 15~30s。热修单文件时同步发布树并留 `.bak-<tag>-<date>`。

---

## 4. 参考路径
- 审计报告：vault `分层讨论/Mímir-全面审计-2026-09-07.md`；本机 `<本机工作目录>\mimir_audit_20260907\Mimir-全面审计-2026-09-07.md`
- 施工计划（已执行勾选）：仓库 `docs/plans/2026-09-07-mimir-full-repair-plan.md`
- 探针/补丁/部署脚本原件：`<本机工作目录>\mimir_audit_20260907\`（`probe_*.py|sh`、`patch_task*.py`、`deploy_v1410.sh`、`task1*_device.*`、`mimir_offsite_backup.sh`）
- 生产真库：`<生产库实例>/canonical.db`（投影 fts.db / graph.db / core_memory.db / chroma/ 同目录）
