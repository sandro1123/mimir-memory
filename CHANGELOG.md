# Changelog · 变更日志

> All notable changes to Mímir are documented here.
> Mímir 的所有重要变更记录于此。
> English · 中文双语

---

## Unreleased (post v14.1.0) — 移交单收尾 (Handoff Follow-ups)

> 接手 09-08 移交单 §2.2/§2.3/§2.4 三件。均无 schema 变更。

- **README 默认语言切换为中文** — GitHub/Gitee 主页默认展示中文：`README_zh.md` 内容升主为 `README.md`（浏览器打开即中文），原英文版挪 `README_en.md`；两文件语言切换链接改 `[English](README_en.md) · [简体中文](README.md)`；`docs/plans/2026-09-08-mimir-doc-locations-and-daily-report.md` 顶层文档清单同步。纯改名+一行链接替换，内容零改动。
- **P0-A 召回诚实判语（v14.2 三 GO 之一）** — `/v8/query`、`/v12/search/trace`、`/v9/search-preview` 三端点顶层加 `recall_verdict ∈ {found, not_found, degraded}`：`degraded`=P46 顶层 degraded 为真（任一相似度通道异常/跳闸，故障先于缺失）；`not_found`=正常检索零命中；`found`=其余。UnifiedSearch 以 `partial` 布尔同源映射。锚通道（铁律安全底）注入的事实按普通命中计——judgment 只看通道健康度与结果数，不做「该不该命中」的价值判断。dashboard 健康灯与「问我的助手」可一词直读。测试 `tests/test_p0a_recall_verdict.py` 8 例（found/not_found/degraded×search、trace 两口径不分叉、unified 同词）。
- **P0-B 治理/结晶欠账重放（v14.2 三 GO 之二）** — 结晶腿（00:30 crystal scan）补上 P48 同构账本：新表 `crystal_runs`（started/completed/failed + error_code + window_days + 计数，V17 迁移链与运行时 DDL 双挂载，守卫式 IF NOT EXISTS 老库自愈）；`CrystalService.scan()` 起手记账 started 并探测断供欠账（failed 且其后无 completed → 返回 `runs.catchup` 计数），成功 completed、异常 failed+error_code 且关账走 connect() 直写降级（主事务炸时账本仍能落 failed）。补跑即重放（scan 本身 upsert 幂等，零特殊路径）；`_exit_code` 的 failed 判定自然联动 systemd 非零退出。断供期三腿（抽取/治理/结晶）至此全闭环「一条不丢」。测试 `tests/test_p0b_crystal_replay.py` 5 例（成功记账/失败记账/欠账补跑/无欠账零动作/新库带表守卫）。
- **P0-C 发布脱敏七面扫描闸门（v14.2 三 GO 之三）** — `scripts/release_scan.py` + 词表外置 `scripts/release_scan_rules.txt`（自指免疫：词表与扫描器自身跳过）+ CI 新 job `release-scan` 每次 push 跑全树。七面=secrets（sk-/ghp_/AKIA/PEM 头）+内网 IP+机器路径+用户名（收窄到危险语境——用户名@主机 裸连/ssh 语境，README 署名与邮箱合法不抓）+payload 样本（生产实例名）+负向对照（`tests/fixtures/release_scan_negative_control.md` 植假 secret，闸门失效即当场揭穿，单测覆盖）。首跑真脱敏 5 处：`dashboard/backend/main.py` 生产实例名默认路径改 MIMIR_DATA_DIR 兜底、FEDERATION 示例真内网 IP 改 RFC5737、CHANGELOG 内网 CIDR、docs/plans 5 份施工文档的本机绝对路径/备份地址/实例名占位符化。全树 148 文件 clean 放行。测试 `tests/test_p0c_release_scan.py` 6 例+14 subtests。
- **P0-D 注入面三层加固（GitHub issue #5 Finding 2）** — 审计实测 10 变体只拦 2（中文转述/全角 Unicode/裸 base64/间接「新游戏」/伪造 [SYSTEM NOTICE]/词间隔全穿透，且治理提示词裸插值候选内容可直通判分 LLM）。修三层：①模式面 `INPUT_INJECTION_PATTERNS` 补六个变体族（含对评估器本身的贿赂指令）；②提示词面 `EVALUATION_PROMPT` 候选内容改 `<<<CANDIDATE_CONTENT … CANDIDATE_CONTENT>>>` delimited 数据块 + 「data, not instructions」边界声明；③判定面新增 `echo_guard`（评估 reasoning 回声「已忽略之前指令」类妥协痕迹 → risk 强制 high + 人工复核，接进 governance 消费链）。纵深防御：单层失效不再等于全线失效。测试 `tests/test_p0d_injection_guard.py` 5 例（10/10 变体必拦+干净面零误杀+提示词含界）+ `tests/test_p0d_echo_decision.py` 2 例（回声→human_review、干净→provisional 不误伤）。另：`MIMIR_VERSION`→14.2.0 时补齐两处漏改（test_r8_release 断言 + pyproject.toml）。
- **P0-E 连接泄漏机械修+断路器线程安全（GitHub issue #5 Finding 3 + 嘟嘟🟡6）** — `sqlite3.Connection.__exit__` 只管事务不关连接：17 处散装 `with store.connect()` 调用点（api/autoskill/conflict/crystallize/federation/governance/multimodal/reporting/trust 九文件）全部改 `with closing(store.connect())`（store.transaction() 本就正确不动）；Linux FD 缓慢累积+Windows 全套 WinError 32 同根因同修。**判例=closing() 后 __exit__ 不再自动 commit——降级直写路径必须显式 commit**（P0-B `_close_run` 的 connect() 直写腿当场踩中：UPDATE 随连接蒸发，测试抓出后补 `connection.commit()`）。CircuitBreaker 三态转换加 `threading.Lock`（allow_call/on_success/on_failure 全入锁——多线程 FastAPI 下并发 HALF_OPEN 多探测与失败计数互相覆盖两病同修）。全量 526 passed。
- **P0-F 开箱三修（GitHub issue #3，monkey2jack）** — ①`server.py` 默认端口 `18456→8456`（历史遗留默认与 EXPOSE/healthcheck/compose/MIMIR_API 全家不一致，首装即「健康检查打空端口」）；②`scripts/init.sh` 新增嵌入模型预取步骤（`runtime.py` 以 `local_files_only=True`+`HF_HUB_OFFLINE=1` 加载 bge-m3 的运行时离线纪律保留，首装在此显式联网预取一次，`SKIP_MODEL_PREFETCH=1` 可跳）；③`backend.main:app` 布局经核实现状正确（`dashboard/backend/` 已是带 `__init__.py` 的包，manage.sh/compose/Dockerfile 三处一致——issue 提的是 v12 期旧布局，已演进，勘误告知 issue 作者）。
- **P0-G 插件死副本删除+契约重写+测试套首次全绿（嘟嘟🔴1/2/3 + issue #5 Minor）** — 删 `hermes-plugin/mimir_memory_provider.py` 单文件旧版（admin-token-for-every-agent、`hash()` 随机幂等键、静默吞异常三病源，v9.2 时代遗物，两位审计者点名）；`__init__.py` 顶部 `agent.memory_provider` ABC 导入加守卫（Hermes 宿主缺失时用结构化 stand-in，clean checkout 可独立导入）；重写契约测试 `tests/test_plugin_contract.py` 6 例——从仓库路径导入（不再依赖 `~/.hermes` 安装态）、ABC 方法面、幂等键 sha256 进程稳定+跨 owner 防碰撞、传输失败结构化 error 非 None、未知工具返回 error。删除过时的 `TestM1dHermesPluginContract`（期待 v12 期 provider.py 单文件布局，与现包形态三面全错）。**全量 532 passed 零 errors——测试套史上首次全绿**（3 errors 预存债清零）。
- **P0-H GOVERNANCE_AUTO_APPROVE 死键通电+联邦诚实标注（issue #5 Finding 4/1 + 嘟嘟🔴7）** — 两审计矛盾（嘟嘟「默认激进」vs #5「死键」）的真相：`governance.py:60` 定义但全文零引用，默认值 "1" 从未生效过；真正自动提交的是 `fast_track_commit_all`（置信度 ≥0.8 免审，worker 治理轮自动触发，历史 282 条经此路径）。修：死键接成 fast_track 总闸——**开源默认 0**（防错误自动晋升污染长期画像），`MIMIR_GOVERNANCE_AUTO_APPROVE=1` 时维持 v14.1 行为；**生产行为不变**（governance unit 显式补设 `Environment=MIMIR_GOVERNANCE_AUTO_APPROVE=1`，钉住既有 282 条的生产路径）。同步诚实标注：README 双语功能矩阵联邦行 ✅→⚙️「已实现未通电 · implemented, not yet wired」（#5 建议，双节点实测落地后摘牌）、v14.0 发布行加同注；FEDERATION.md 修 `append_event` 示例真键契约（旧示例 `crdt_key`/`event_id` 复制即报错）与「账本静态加密」错误表述（加密在信封层，账本列明文——at-rest 加密不适用于该层）。测试 `tests/test_p0h_fast_track_gate.py` 3 例（默认关/门关阻断/显式开）。
- **P0-I AGENT_IDS 开箱可扩（GitHub issue #1，monkey2jack）** — issue 提的 v12 期闭集阻塞经核实已部分演进（`register_agent`/`register_domain` 动态 API v14 已存在）；本卡补齐开箱面：`MIMIR_AGENTS`/`MIMIR_DOMAINS` 环境变量逗号清单启动期扩册（保留四席默认 fallback），worker CLI 新增 `register-agent`/`register-domain` 命令（清单回显）。未注册者仍 fail closed——开箱可扩≠ACL 放宽。测试 4 例（默认不动/env 引导新 agent 可建 fact/运行时注册直通/未注册仍拒）。
- **P0-J migrate_cli 12→全链续修（GitHub issue #2，monkey2jack）** — 病确认：`migrate_cli` 把 source=12 分流去单腿 `migrate_schema_v13`，只应用 v13 DDL 就盖章 `SCHEMA_VERSION`（审计时 18、现 20）——v14~v18 结构从未创建，runtime 按 schema_version 信任库却缺表。修：12 与其他版本一律走全链 `migrate_schema()`（additive 链从 V13 起步顺序补齐）；`--to-version 13` 显式降级请求保留单腿。测试 2 例：从 CanonicalStore 建库降戳到 12+DROP v13~v18 表造真实前身，断言迁移后 v13~v18 六组代表表全在且版本=20。夹具判例：迁移链只管 additive 层，v12 实库本来就带 v10 核心表——裸 sqlite 夹具必被防御检查正确拒绝。
- **P0-K 容器部署面安全加固（嘟嘟🔴4/5/6+🟡8）** — 四件：①Compose API 与 Dashboard 端口默认绑 `127.0.0.1`（LAN/WAN 暴露须显式改映射并自担 ingress 边界；容器内 `MIMIR_ALLOW_NONLOOPBACK=1` 由 compose 显式授予，镜像默认改 0）；②Dashboard 弃「运行时 pip install 无锁定版本」，改走现成 `dashboard/Dockerfile` 构建期安装（requirements.txt 固定清单）+ 固定 entrypoint；③Dockerfile 清华镜像源硬编码改 `ARG PIP_INDEX_URL`（默认官方 PyPI 供应链可审计，国内网络构建时显式传参）；④镜像 healthcheck `/health`→`/ready`（ready 检查 auth 注册数+投影积压+死信，「端口活着≠记忆可用」，加 start-period 60s）。requirements 核心依赖下限抬到生产实测版本（fastapi 0.141.1/chromadb 1.5.9/cryptography 50.0.1/pydantic 2.13.5/httpx 0.28.1——全量精确锁以发布树 venv 的 pip freeze 为准）。
- **P0-L 联邦协议硬化五点（GitHub issue #5 硬化附注）** — #5 实测四病全修+载荷纪律：①密钥持久化——`federation_local_key` 表（守卫式），重启复用本节点钥（旧形态每实例重新生成，服务重启即全 peer 注册作废）；②`ingest_envelope` 收件人校验——外层 `to_peer`≠本节点拒收（fail closed，不再静默入账误投/劫持件）；③事件署名三面放行=已注册 peer/发送者本身/本节点账本已见节点，第 4 面即伪造（#5 的「C 持己钥伪造 A 署名」攻击在此拦截）——判例=署名者≠发送者是 CRDT 中继合法形态，过严会打断转发路径；④lamport 上界 10^8（逻辑计数器真实量级；#5 实测攻击值 1e9 恒久删除任意键且无 tombstone 回滚路——初版界设 2^31 太高没拦住攻击值，收到 10^8）；⑤单 envelope 事件数上限 5000（内存面纪律）。测试 5 例（持久化跨重启/误投拒收/伪造署名拦截/界值/载荷上限），存量 12 例零回归。
- **P0-M dedup 召回性能改造（嘟嘟🟡1/2）** — `check_duplicate` 弃全表扫：入站内容取最长 6 个 token 作 LIKE 探针召回候选池（cap 50）再精排 Jaccard——写入路径从 O(N) 全库降到 O(召回池)；降级纪律=探针空手而 owner 有事实时回退全表扫（正确性优先于性能）。tokenizer 补实体 token 三类：数字段（3.8/1.5.9）、v 前缀版本（v14.2）、复合型号 id（gemini-3.8-flash）——「Gemini-3.8-Flash vs Gemini-4.0-Pro」的型号差异不再被中文 bigram 稀释磨平。测试 6 例（实体 token 保真/型号差异拉低相似度/真重复 merge/子串级 update 段/远域 new/召回 cap 下正确性）。全量 552 passed。
- **P0-N TokenStore 原子写协议+reload 可观测（嘟嘟🟡3；🟡5 核实为已完成）** — 嘟嘟🟡5 的 ASGI body 上限核实**已有**（P1-6 中间件：Content-Length>1MiB → 413，非缺口）；本卡补 🟡3：新增 `mimir_v8.auth.atomic_write_registry`（运维侧 token 表唯一合法写法：同目录临时文件+fsync+`os.replace` 原子改名——直接 `echo >` 会让 mtime 触发式热加载在中间态读到半截 JSON → auth_unavailable 全站 503）；TokenStore reload 失败补 WARNING 日志（path+异常类型+指引，fail closed 语义不变、静默面消除）。测试 3 例（原子写往返/替换无中间态/损坏 JSON fail closed）。
- **P0-O 版本域治理+Release 页首发（嘟嘟🟡7 + Minor + 用户点名）** — ①历史 tag 补齐五枚（v12.0.0=92c41b7 开源首发/v12.0.1/v12.0.2=eee7c3d 安全修复/v12.2.0=4842e11 末件/v13.0.0=7f44a28 收口），双平台推平——8 版断代史终结；②README 新增「版本号域表」：五域（Release/Schema/API 代数/包名/仓库名）各语义与修改位一次说清；③`tests/test_p0o_version_domains.py` 三方对拍断言（MIMIR_VERSION↔pyproject↔CHANGELOG 版本段，漂移即红——开发中当场抓到两处漏改的实证）；④衰减层级勘误：文档「五条遗忘曲线」→实况六级（L0_never+五曲线）；⑤GitHub Release 页 v14.2.0 首发（aduMEI 式细粒度：每卡一段、file:line 证据、前后行为对照、质量账本表、升级路径）。
- **P0-P 死代码+文档漂移六件（移交单 §2.7 残留清账）** — ①删 `mimir_v8/coalesce.py`（零引用零测试尸块）；②`migrate_schema_v14/v15` 补历史台阶注（生产路径=migrate_cli 全链，单腿函数是版本标+单测锚非死代码——防再误判）；③ARCHITECTURE 三处漂移修正：§10/§11 标题 schema 19→「19→20」/「20」（两版混写）、`/v10/opinions/consolidate`→`/v10/observations/consolidate`（真路径）；④TKG 列名 `valid_during`→`valid_from`/`valid_until`（对齐 store.py 实况）；⑤README 新增「环境变量速查」：约 56 键分四组语义表，重点钉死两对「看似重复实为分工」的活键——`MIMIR_HERMES_STATE_DB`（worker CLI 面）vs `MIMIR_CONNECTOR_HERMES_STATE_DB`（config 装配面）、`MIMIR_EVAL_API`（自评套件）vs `MIMIR_EVAL_API_URL`（治理 LLM）——文档缺位曾致两轮误判为漂移死键；⑥FEDERATION.md §209 注释与 append_event 真键契约对齐。TKG 静态遗物与 13 张零行表**留守待知识层通电卡（#38）一并裁**（删表属破坏性且 candidates 冲突消解活用 relations 写路径）；mcp.py 定谳**保留**（TestR7V12MCP 活测+MCP stdio adapter 有宿主消费面）。全量 558 passed。
- **v14.2.1-1 知识喂料闸门开刀（09-11 盘点 2902 积压正主）** — `llm_extract_once` 源闸门从硬编码 `conversation` 改可配置：`MIMIR_EXTRACT_CATEGORIES`（call-time 读取，默认 conversation 生产行为不变；扩册 `conversation,knowledge_doc` 即放行 vault/rss）。**盘点勘误**：2902 条 stored 里大部分正文已被 retention 清掉（rss 78%/vault 92% '[RETAINED CONTENT PURGED]'，真可抽仅 ~586 条），故配套两刀：①壳 run 诚实清账 `_settle_purged_shell_runs`——purged 的 stored run 标 `extracted+content_purged`（幂等限 500/轮，账本可审计，积压水位回归真实不再虚胖 2300+）；②purged 防线——抽取 SELECT 排除已清正文，绝不进 LLM。测试 3 例（默认闸门不变/env 扩册 vault 入队/purged 清账留痕）。施工中 P0-E 判例二次复发实证：settle 初版在 closing(connect) 只读块里 UPDATE——closing 后无自动 commit，蒸发；改自带事务后绿。
- **v14.2.1-2 RSS 日配额 + 批量消化模式** — `_extract_source_quotas`：按 connector 的每日（UTC）抽取配额，默认 `rss:100`（用户 09-11 拍板——vault 全量先吃、rss 限速防 LLM 调用打爆 cbcn 倍率池），`MIMIR_EXTRACT_DAILY_QUOTA="rss:100,vault:0"` 可改（0=不限，显式设 env 即完全接管）。实施面=Python 侧计数过滤（当日 extraction_runs completed/cancelled 计数 vs 配额），非 SQL correlated 子查询（初版那个 join 三层反查太脆已弃）。批量消化：`llm-extract --limit` 上限 50，积压补跑用 `--limit 50` 多轮跑（586 真实积压 × 3~6 轮/日即可清完，每轮都是常规轮次的放大版零特殊路径）。测试 3 例（默认 rss:100/env 覆写/显式清空即接管）。全量 564 passed。
- **v14.2.1-3 长文抽样 + LLM 闭集守卫（vault 首批实测 ValidationError 根因修复）** — 开闸首轮实测暴露两病：①vault 笔记上万字符整段喂 evaluator，fallback 截 200 字=截断 YAML 头，长文尾部信息全丢——修=超 4000 字符取首尾各 2000（省略号分隔），保住元数据头与结论尾两个信息密集带；②LLM/evaluator 返回的 domain/fact_type 可能超出 DOMAINS/FACT_TYPES 闭集，`CreateFact` 校验直接炸整条 run（实测 10 条 ValidationError）——修=闭集守卫落回合法默认（domain→personal、fact_type→reference）+ WARNING 日志可观测，单条不再全损。测试回归 564 passed。
- **v14.2.1-4 服务层第二道闸门对齐（vault ValidationError 真根因收官）** — 生产复跑仍炸 ValidationError，verbose 栈抓到真根因：`extraction.py:_validate_main_source` 里藏着**第二道硬编码闸门**（`source_category must be 'conversation'`）——v14.2.1-1 只开了 worker 选队闸门，服务层校验闸门把每条 vault/rss run 拒死，单条错误消息带 run_id 但被 worker 捕获折叠成 ValidationError 计数（教训：**改闸门类约束必须 grep 全链所有 gate 面**——r3 gate 测试三断言同步对齐）。修：服务层闸门读同一个 `_extract_categories()`（单一事实源，两道门永不再漂移）。测试 3 例（闸开 knowledge_doc 过/默认 conversation 过/闸外仍拒）。全量 567 passed。
- **v14.2.1 生产实施回执（09-11）** — extract unit `MIMIR_EXTRACT_CATEGORIES=conversation,knowledge_doc,external_info` 开闸；三轮真跑验证：①首轮暴露 ValidationError×10 → verbose 栈实锤 `extraction.py` 第二道硬编码闸门（v14.2.1-4 已修）；②修后复跑 `count:1 failed:0 mode:llm`——vault「QuantStar 事故簿」笔记成功抽出知识候选进治理链（review_required），run 终态 extracted+completed；③积压水位回归真实：`content_purged` 壳 run 诚实清账 **2350 条**，剩真实可抽 rss 534 + vault 36（rss 100/日配额自然限速中）。喂料三面（conversation 持续 / vault 已通 / rss 限速通）**全部通电**——09-07 审计的「喂料零新事件」残留病就此销案。
- **P48 抽取链 LLM 失败语义化（§2.2，cd31055）** — `EvaluationResult.llm_unavailable` 显式标记；`llm_extract_once` 遇标记记 `extraction_runs.status=failed + error_code=llm_unavailable` 且 ingestion 保持 `stored` 供下轮重试（此前 LLM 抖动=对话永久丢弃且与正常 discard 不可区分）；24h 内同 run 失败 ≥3 次退避出队；返回带 `llm_unavailable` 计数。ops health 新增「近 2h 抽取全 failed 即报」哨兵。
- **symbolic offload 空文本入口拒绝（§2.4，5fbce55）** — `raw_text` strip 为空即 422；生产冒烟残留空块 `sym_10f7f8e6` 已按 block_id+length=0 双条件清除（empty-blocks 0）。
- **生产 mimir_config.yaml 死键清理（§2.3，生产仓 7e481aa）** — 4142B→1198B，只留 `version/collector/reflect/federation` 四段真消费面（`reflect.topics` 为 ops/weekly_reflect.py 所读，审计清单勘误补入）；collector 真跑 RSS 4 源+vault 全绿验证；原件备份 `backups/mimir_config.yaml.bak-v7full-20260908`。

---

## v14.2.0 — 2026-09-08 · P0 三小卡 + 中文默认 README (P0 Trio + Chinese Default README)

> 2026-09-08 用户三 GO 拍板当晚交付。Schema 保持 20、无迁移、无 reproject。
> P0 详见 Unreleased 段各条；本版本另含 README 默认语言切换。

- **版本面** — `MIMIR_VERSION = "14.2.0"`；生产以发布树 `v14.2.0-20260908` 滚动上线。

## v14.1.0 — 2026-09-07 · 全面审计修复 (Audit Remediation)

> 2026-09-07 全面审计（1 P0 + 12 P1）的修复包。Schema 保持 20、无迁移；生产以新发布树 `v14.1.0-20260907` 滚动上线。

- **P0 治理 LLM 静默失效 6 天** — `governance.router_config()` 运行时解析并回退到 `MIMIR_EVALUATOR_*`（治理单元从未配 `MIMIR_ROUTER_*`；v10 硬编码 key 被开源脱敏后治理无 key 运行、18 条候选每 15 分钟 requeue 一轮）；`_call_llm` 返回 `(result, cause)` 并记 WARNING，`error_code` 带真实原因；`review_requeue` 24h 内失败 ≥3 次不再弹回；治理结果带 `llm_failures`；`worker.main()` 按结果返回非零退出码；新建库补 `candidate_review_assessments`/`governance_decisions` DDL（此前只存在于生产库）。
- **投影漂移** — FTS 投影 no-op 守卫加 status 比对（同版本纯状态变更曾被跳过，3 条 disputed 事实在 FTS/graph/vector 里仍 active）；新增 `operations.reproject_facts` + `mimir-migrate reproject` 离线修复 CLI。
- **人审语义** — `commit_approved` 按 `reviewed_by` 决定 `human_status`：人（非 `service:*`）批准 → `confirmed`，自动批准 → `unreviewed`。
- **P45 收尾** — `build_runtime` 传入 `error_hook`，投影线程异常进日志（此前被接住却无声）。
- **Hermes 插件** — 仓库副本同步生产版（`mimir_feedback` / 注入防护 / `_owner`）；读写路径改用 per-agent token（ACL 在召回面生效），`mimir_remember` 返回 `{ok, error}`，幂等键改 sha256，`mimir_reflect` 发 `text`；plugin.yaml 14.1.0。
- **Dashboard 4.1.0** — 目录改为 `dashboard/backend/main.py` 与部署树一致（Docker/compose/manage.sh/README 从此可用，`FRONTEND_DIR` 默认值随之正确）；`/v11/symbolic/offload` 装饰器修复注册；删除误装饰到 helper 的 `/v10/opinions|observations` 假路由；review 端点仅 POST；所有写端点 `_invalidate_all()`；无密码配置时会话密钥用进程随机值；`_mimir_get/_post/_db_query` 记日志；前端 fetchJSON 出错显示角标；删除死副本 `dashboard/governance.py`。
- **交付面** — `scripts/init.sh` 生成独立 `admin.token`（agent 仅 read/write），config 模板改为代码真正读取的 `federation.*`/`collector.*`；QUICKSTART 改用 `MIMIR_V8_*` 变量并带 Bearer；Docker 镜像 `--bind 0.0.0.0 --port 8456` + `MIMIR_ALLOW_NONLOOPBACK=1`；SECURITY 支持矩阵更新；ROADMAP P1 勾选；`pyyaml` 显式声明。
- **随包** — #41-A vault 笔记双路由进 wiki 知识层；金标健康哨兵 `mimir_v8.eval_suite --golden-health` + `UpdateFact.decay_tier`。
- **部署后热修（2026-09-08，发布树同步）** — `SymbolicMemoryService` 构造时自愈建表并守卫式补 `owner_principal`：生产 `symbolic_blocks/symbolic_canvases` 是 v8 老形态，`V14_ADDITIVE_STATEMENTS` 只被从未接线的 `migrate_schema_v14` 引用，`/v11/symbolic/offload` 在生产 500（看板路由修好后首次暴露）。

## Unreleased (post v14.0.0) — 生产上线与运维修刀 (Production Rollout & Hardening)

> v14.0.0 后的部署与稳定性系列，均为生产部署树/服务配置层修复，无核心代码迁移。

- **v14 生产迁移收官（2026-09-03）** — 生产 API v12.1.4 → v14.0.0 + Schema 19 → 20（relations 时态列，483 行零丢失）；v14 三件（AutoSkill/联邦/投影）全部在生产通电验收；三笔运行时修复：wake 接线（f2fb822，503 gap）、graph 传参（5921bbc，503 gap）、旧库 graph_edges 守卫式 ALTER（7f44a28，500 gap）——「建了没通电」三连修。
- **Dashboard v3.0.1（2026-09-03~04）** — 技能/联邦/投影三新页接通生产；三稳定性修刀：①活动 tab 后 7 面板被吞（grid 双开标签）②60s 周期性 Chart.js 崩溃（Alpine 深层代理打断 Chart.js 内部 Map 的 raw 实例键——chartInstances 搬出 reactive 状态，ce4a0a5）③黑板页直查 v13 blackboard.db（4584db9）。
- **生产部署面三修（2026-09-04）** — ①dashboard systemd unit `--host 127.0.0.1 → 0.0.0.0`（重启后 LAN 访问复断的地雷）②ufw 放行 8800 仅限 `<内网 CIDR>`（例：192.168.x.0/24）（default deny 下从未放行——LAN 不通的真凶）③退役手工启动进程，systemd `Restart=always` 正式接管 8800（消除双进程 crash-loop）。
- **Dashboard v4.0.0 客户视角重构（2026-09-07）** — 13 tab 开发者控制台 → 默认 3 tab 客户视图（今天/记忆库/设置）+ 开发者模式开关（localStorage `devMode`，旧 13 面板零丢失一键还原）；三聚合后端 `/api/dashboard/today|library|health-light`（TDD 8/8，partial 降级不塌骨架、空态≠降级）；今天页时间线（学到/待审内联确认/淡忘）+「问我的助手」搜索（复用 trace 通道）；记忆库卡片流三筛选+分页+待审角标；侧栏健康灯四态（绿/黄/红/灰）可展开原因；客户模式 60s 轻载（只拉灯+今天，不打 4 重端点）。真库判例五条钉死入代码注释：facts 无 created_at（真名 recorded_at）、「有分歧」是 status 值非列、待审真状态 human_review（review_required 生产恒空）、来源须 join sources 取 source_kind（title 多 NULL）、`data/canonical.db` 是 0 行空壳诱饵（真库在 v9/production-*）。commit 4182282（后端）+ 7ec2adb（前端）。
- **Mímir-Eval 生产金标复测（2026-09-04）** — 生产 v14.0.0/Schema 20 实跑：`failed_floors=[]`，hit_rate@10 0.875（贴地板）→ **1.000（脱地板）**，hit_rate@3 维持 0.750 地板——分层装配+锚通道对召回无退化且 @10 改善；@3 零裕度根因定位为金标事实老化（unreviewed/零置信/L4 衰减档），v14.1.0 治理项。
- **P45 ProjectorSupervisor 韧性修刀（2026-09-07，61d10d9）** — 生产事故：09-03 03:39 UTC 迁移重启窗口 sqlite 锁竞争使 drain_once 内 runner 抛异常，`_run` 主循环无保护 → daemon 线程静默死亡（无日志无自愈）→ outbox 永久积压 → `/ready` 恒 503 → mentor 巡检每 15 分钟飞书报错 4 天（3392 次）无人知。修复三层：drain_once per-runner 保护（单投影器异常降级 0 进度+failed=1，其余不受影响）+ error_hook 观测钩子 + `_run` 外层兜底 try/except。验收：重启后 outbox 积压 8→0、checkpoint 13029→17703、`/ready` 200、ops health `ok:true`、巡检脚本静默。checkpoint 与 event head 间 301 条为 candidate.*/conversation.* 旁路事件（历史上从不入 outbox，零投影语义，良性）。测试 `test_p45_supervisor_resilience.py` RED→GREEN 2/2；全量回归 436 passed 零回归。
- **Dashboard devMode 回程修刀（2026-09-07，ba8ca2d）** — 用户报告：开启开发者模式后无法返回客户模式。根因两处叠加：设置面板容器带 `x-show="!devMode"`（开关所在面板被自己隐藏，单向门）+ navItems 13 项无 settings 入口。修复：设置面板两模式共用 + navItems 追加 settings 第 14 项。
- **P46 检索通道三态断路器 + 诚实遥测（2026-09-07，cd9cf41）** — v14.1.0 P1 韧性挡位。现状病（P45 同族）：vector/fts/graph 三相似度通道无 per-channel 隔离，chroma/fts 后端一次抖动整个检索面 500；channels 只报 enabled 布尔无降级感知。施工三件：①CircuitBreaker 三态（连续 3 次失败→OPEN 跳过调用止损，60s 冷却后 HALF_OPEN 放一次真实探测，成功回 CLOSED）②通道隔离（单通道异常只降级该通道，锚通道不走断路器——直查 canonical 是全后端炸时的安全底线）③诚实遥测（channels 布尔→closed/degraded/open/off 三态 + 顶层 degraded 布尔，search/trace 两口径不分叉）。RED 8→GREEN 8；全量 444 passed；生产验收 degraded=False + 五通道 closed + 真检索 5 结果。

## v14.0.0 — 2026-09-03 · WikiSkill 技能流水线 + 加密联邦 + 跨模型投影 (AutoSkill · Encrypted Federation · Cross-Model Projection)

> 三件功能 + 升版收尾，全零迁移（无 DDL 变更），SCHEMA_VERSION 维持 20。

- **WikiSkill 技能自动编译流水线**（`autoskill.py` + `/v14/skills/*`，fcd45da）：Traces (L0) → Mímir Wiki (L1/L2) → Hermes Skills (L3) 三层演化链。`record_success` 按主题沉淀成功 trace（幂等 per trace；拒绝 unknown/非 active trace，Fail-Closed）；胜任门槛 = 成功 ≥3 且成员零 negative feedback；`compile_wiki_candidates` 出列候选；`promote_to_skill` 一键审批物化 L3 skill fact（promotion 时再验门槛——ledger 可能已变；幂等：重复晋升同一 fact）。skill 入 `ANCHOR_FACT_TYPES`/`LAYER3_FACT_TYPES`——检索面自动全量挂载，与铁律同一存在保证。REST 面 `write`/`read`/`manage` scope 门（ingest-only 403）。
- **跨节点去中心化加密联邦**（`federation/`，acea7a9）：多台家庭服务器（N100/台式机/云端节点）基于 append-only CRDT 事件流加密同步。federation_events 每次变更一行携带 lamport 时钟+node_id；冲突按 LWW 合并（lamport 高者胜，同刻比 node_id DESC——全序无分叉）；离线容灾：断线期间各自写入，重连按 since 游标交换事件流增量重放，合并可交换（A∘B == B∘A，最终一致）。加密信封 Fernet：出节点加密、入节点按注册 peer 密钥解密——未注册 sender 的密文无法解密（Fail-Closed），篡改信封拒收。`(crdt_key, lamport, node_id)` UNIQUE → 重投递 no-op。federation_peers 注册表带密钥指纹（sha256 前 9 字节 base64，人工核对握手凭据）。
- **跨模型认知语义投影**（`projection.py`，cd5055d）：适配不同模型窗口与输出格式，实现小模型挂载优质技能后的越级能力爆发。MODEL_TIERS 三档：claude（大窗 8k，全保真 markdown）> deepseek（中窗 3k，结构化列表）> local-small（小窗 1.2k，紧凑 KEY: value 方言）。`project_context` 把同一检索面投影成目标模型注入块：L3（iron_rule/user_pref/skill）content 全保真——锚通道保证穿越投影存活，技能永不裁剪（越级能力的全部来源）；L2 按档降级（全文→摘要→硬截断）；L1 所有档只留类型+溯源行（fact_id 可溯源不占预算）。预算守卫从尾部先丢 L1 再丢 L2，永不丢 L3。token 估算保守 2 字符≈1 token，截断必带省略号（无静默截断）。
- **版本号 13.0.0 → 14.0.0**（三锚定：`schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言；SCHEMA_VERSION 维持 20）。

## v13.0.0 — 2026-09-03 · 共享工作黑板 + 时态知识图谱 + 主动前置唤醒 (Blackboard · Temporal Knowledge Graph · Proactive Wake)

> 三件功能 + 升版收尾。SCHEMA_VERSION 19 → 20（relations 增 `valid_from`/`valid_until` 双列，守卫式 ALTER，旧库平滑升级）。

- **多 Agent 共享工作记忆黑板**（`blackboard.py` + `/v13/blackboard/*`）：多 Agent 在排障/分析/研讨时秒级共享局部任务上下文；board 参与者边界（非参与者读写被拒+入参防伪造）、distill 提炼总结沉淀为长期事实、destroy 安销（留 audit 痕）；creator-not-participant 回滚守卫。REST 面 `write`/`read` scope 门。
- **时态知识图谱 TKG**（`schema.py`+`store.py`+`migration.py`+`graph_projector.py`，a0aa913）：relations 增 `valid_from`/`valid_until` 时效窗（空串=开放区间）；supersede 写双向边（supersedes 开窗 + superseded_by 零宽关窗）；`/v13/graph/history?at=ISO8601` 时点快照查询。通用迁移链 `<=18` 恒 rebuild，专用 `migrate_schema_v19` 冻结产出真 19 形。守卫式 ALTER 对新库降 stamp 夹具免疫。
- **主动意图预测性前置唤醒**（`relevance.py` + `/v13/wake`，65e35d3）：IntentProfiler 关键词驱动意图分类（destructive/change/troubleshooting/generic，轻实现不依赖 LLM）；ProactiveWake 前置推送铁律+核心偏好（任何意图永远推送，安全底线）+同意图家族 pattern（排障意图推排障 pattern；generic 不推，宁缺毋滥）；全程过 `can_read` ACL 仲裁（Fail-Closed）。
- **版本号 12.2.0 → 13.0.0**（三锚定：`schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言；SCHEMA_VERSION 断言随 TKG 19→20 对齐）。

## v12.2.0 — 2026-09-03 · 记忆分层装配 + 检索免疫双通道 + 血缘继承 (Layered Assembly · Anchor Channel · Lineage Inheritance)

> 五件全零迁移（无 DDL 变更），SCHEMA_VERSION 维持 19。

### Added · 新增
- **L0~L3 分层装配与渐进式展开（spec 阶段二任务1）** — 检索默认只装配 L3（iron_rule/user_pref）+L2（pattern），L1 原子事实（event/project_config/ephemeral/learning/reference）standard 档不装配、`depth="deep"` 才下钻，大幅削减 Token 消耗；L0（conversation_messages 原始对话）作为证据层检索永不装配。映射零迁移：全部复用 facts.fact_type 既有枚举。分层注入走统一 layer sweep（standard 扫 L2、deep 扫 L2+L1，`LAYER2_BUDGET` 预算内保保存），显式 `fact_type` 过滤器覆盖深度默认（表达精确追溯意图时放行）；hydration 段 L1 门把相似度通道漏进来的 standard 档 L1 拦下并计数 `filtered["layer"]`（可观测）。`trace()` 严格镜像 search()（同 sweep 块+同 L1 门），新增 `LayerSweep` 漏斗阶段，两口径不分叉。
- **检索锚通道（Anchor Channel，spec 阶段二任务4）** — 铁律与用户核心偏好免被语义相似度一票否决：锚通道在候选池构建阶段直接从 canonical 注入活跃 iron_rule/user_pref，不依赖 vector/fts/graph 三通道命中。ACL 仲裁与状态过滤照常在 hydration 执行（锚通道改变「谁能进池」，不改变「谁能被读到」）；`use_anchor=False` 可关；注入量受预算约束（防铁律库膨胀挤占 top-K）；trace() 报告 `AnchorChannel` 阶段（hits/injected/enabled）。
- **统一 Profile 视图 API（/v12/profile，spec 阶段二任务2）** — 跨 L3~L1 一站式只读聚合：iron_rule/user_pref/pattern/event/project_config/learning/reference 按 owner+domain 过滤直查 canonical，带 ACL，为上层「人格视图」消费提供单一入口。
- **XTMEM 血缘最严继承 + Fail-Closed（spec 阶段二任务3）** — supersedes 链上 visibility/sensitivity/egress_policy 三档各取 max(来源，提案)——继承自来源且提案永不放宽；幽灵来源（supersedes_fact_id 不存在）Fail-Closed 抛 `CandidatePolicyError`（422 面），schema FK 是最后防线（500 面）；继承落在 proposed_* 列，commit_approved 直读即贯穿。`create_candidate_in_transaction` 单一卡点全链覆盖。

### Fixed · 修复
- **disputed 投影同步闭环（spec 阶段二任务5）** — 两处真凶双杀：①`_mark_disputed` 裸 INSERT `memory_events` 无 outbox 行——`fact.conflict_lost` 永不进投影流，fts/graph/vector/core_memory 四投影继续按 active 服务败者（读到已被否决的事实）；修法=对齐 store 既有 `_insert_version_and_side_effects` 先例，向 `PROJECTORS` 全扇出 pending。②conflict.py 三事件 payload_hash 是自造串（非 `sha256(payload_json)`）——`verify_canonical` 一致性门禁对冲突事件必报 `event_hash_mismatch` 误报；修法=三处改对 payload 本身求哈希，verify 误报清零。

### Changed · 变更
- **API 层透传 v12.2.0 检索参数（收尾件）** — QueryBody 增 `depth`/`use_anchor` 两字段，`/v8/query` 与 `/v12/search/trace` 两构造点透传进 QueryRequest；此前 REST 调用方被永久锁死在 standard 档+锚常开——内核能力已存在但对 API 使用者不可达。`/v8/query` 对非法 depth 答 422（对齐 dedup_threshold 先例）。
- **版本号 12.1.4 → 12.2.0**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。SCHEMA_VERSION 维持 19——五件全零迁移，无 DDL 变更。

---

## v12.1.4 — 2026-09-02 · 采集管道三缺口补全 + Schema v19 (Collector Wiring + Schema v19)

### Fixed · 修复
- **vault 采集物分类补键（通电前审计发现）** — `classifier.SOURCE_CATEGORY_MAP` 无 `"vault"` 键，vault 笔记经 collect_all 摄入后落 `unknown/quarantine`——隔离数据无下游消费者可用。修复：vault 归类 `knowledge_doc`（与 feishu/file/document 同族本地知识文档；`KNOWLEDGE_DOC_TYPES` 同步），extraction 闸门（仅放行 `conversation` 类）自然将其挡在 LLM 提取之外——vault 全文只落库不外呼。
- **collect_all 透传 per-source `exclude_dirs`** — `worker.py` vault 分支未把 config 的 `exclude_dirs` 传给 `VaultCollector`（只传 vault_root），生产 vault 含明文凭据目录（敏感扫描 7 文件命中）无 config 层排除手段。修复：per-source `exclude_dirs` 与内置默认四目录（.obsidian/.git/.trash/.smart-env，`DEFAULT_EXCLUDE_DIRS`）取并集——配置的排除名单不会静默丢掉默认项。
- **web 源幂等键加内容指纹** — `web:<sha256(url)>` 只锚 URL：页面内容更新后第二次采集必撞 `ConflictError`（"idempotency key was reused with different content"）进 `results["errors"]`，web 源通电后首内容变更即断流。修复：key 改 `web:<sha256(url)>:<sha256(content)>`，内容变更采集为新版本，同内容仍幂等去重。

### Added · 新增
- **Schema v19：conversation_sources connector_type 解除旧 CHECK 冻结（vault 首采实弹发现）** — 生产库 v8 建库时 connector_type 冻结在七个旧类型的 CHECK 白名单，vault 首采（2026-09-02）全量 `IntegrityError`（415 篇零落库）；dev 新库 DDL 无此 CHECK（宽松），测试全绿掩盖了生产拒绝——新旧库 schema 漂移。修复方向判例（首版收紧、门禁 7 真红复盘后定稿）：**classify() 拥有未注册类型的 quarantine 路由权，DDL 不设卡**——生产 v8 库重建后与 dev 新库一致为无 CHECK 宽松态（7 个存量 unknown_xyz 测试锚定「可插入但被隔离」契约）。修复件：`schema.py` SCHEMA_VERSION 18→19；`migration.py` 新增 `migrate_schema_v19()`（表重建：SQLite 不能 ALTER CHECK，新表→拷行→改名，行数 before/after 校验 + foreign_key_check，沿 12-step ALTER 惯例），主链 `migrate_schema` 白名单放宽至 {9..18}→{11..19} 并在 additive 链后接 v19 重建。

9 项新测试（tests/test_p22_collector_wiring.py，含 legacy CHECK 造库→迁移→vault 可插入端到端 + 新库同约束）。RED→GREEN：三缺口 5 红 + v19 2 红 → 绿 9/9。

### Changed · 变更
- **版本号 12.1.3 → 12.1.4**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.3 已推公共远端不可重写，且锚定的是三缺口修复（28cd706）、不含其后的 v19 表重建三笔——Schema v19 以独立 tag v12.1.4 锚定部署树。

---

## v12.1.2 — 2026-09-01 · 写路径注册表半通电补全 (Write-Path Registry Completion)

### Fixed · 修复
- **静态集消费者全迁动态注册表（部署实弹验收发现）** — v12.1.1 通电了 `validate_agent_id()` 半边，但 6 处写路径消费者仍查静态 `AGENT_IDS`/`DOMAINS` frozenset：quantstar 经 config federation 注册后仍被写路径拒收（生产活进程铁证：`POST /v8/facts` 422 `invalid owner_principal: quantstar`；DB 中 quantstar 存量 15 条系旧热修时代写入，新写入被堵）。修复：全部迁移到 `get_registered_agents()`/`get_registered_domains()`——
  - `schema.py` `CreateFact.validated()`（owner+domain 两查，POST /v8/facts 主写路径）
  - `learning.py` `remember()`（显式记忆摄入，agent+domain）
  - `core_memory.py` `promote()`（canonical 晋升闸门）
  - `api.py` `crystal_approve`（回落 mentor 前先查动态集——注册 agent 不再被静默改派）
  - `knowledge.py` `create_item`（domain 闸门，grep 复查新发现）
  - `evaluator.py` domain 白名单（`ALLOWED_DOMAINS = frozenset(DOMAINS)` 导入期冻结——改为 `_allowed_domains()` 每次评估现算，grep 复查新发现）
  负向守护：未注册 agent/domain 依旧拒收不变。11 项新测试（tests/test_p21_federation_write_paths.py），含「消费者模块不得 import 静态集」源级总闸。

### Changed · 变更
- **版本号 12.1.1 → 12.1.2**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.1 已推公共远端不重写，热修以独立 tag v12.1.2 锚定部署树。

---

## v12.1.1 — 2026-09-01 · 动态注册表通电 (Federation Registry Wiring)

### Fixed · 修复
- **动态注册表通电（部署前审计发现）** — `register_agent()/register_domain()`（v12.1.0 任务1，d91b0fc）建成后全库零调用点（教科书式「建了没通电」），部署 v12.1.0 将使 quantstar（生产硬编码热修注册）写入直接校验失败。修复：`config.py` 新增 `load_federation_registry()`——读 `mimir_config.yaml` 可选 `federation.agents/domains` 段逐项注册；缺文件/缺段静默跳过（worker 无 config 可跑），结构错 ValueError 决不静默（铁律#12）。挂点双入口：`worker.main` 与 `runtime.build_runtime`，server/worker 进程全覆盖。8 项新测试（tests/test_p20_federation_bootstrap.py），注册表模块级集合有快照/还原隔离。

### Changed · 变更
- **版本号 12.1.0 → 12.1.1**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.0 不重打（公共远端已发布），热修以独立 tag v12.1.1 锚定部署树。

---

## v12.1.0 — 2026-08-31 · Eval 安全网 (Eval Safety Net)

### Added · 新增
- **Mímir-Eval 评测套件** — `mimir_v8/eval_suite.py`。纯函数指标层 + 种子化合成基准。
  - 检索指标：`hit_rate@K`（查询级：任一 ground-truth 进 top-K 即 1.0）与 `mrr`（仅首个命中：1/rank，未命中 0.0）；空 ground_truth 拒绝计分（ValueError）。
  - 抽取指标：precision / recall / F1，集合语义（重复只计一次）。
  - ACL 泄漏率：检索行中未授权占比——生产地板值为 0.0，任何非零值即安全回归。
  - **诚实遥测**：合成基准在报告与摘要两级均盖 `provenance="synthetic"` 章，合成数字永远无法伪装成生产质量；真实地板值在在线金标集（tests/test_r9_eval.py），不在此处。
  - 双语（CJK+latin）12 主题合成语料 + 固定种子可复现；19 项新测试（tests/test_p18_mimir_eval.py）。
- **全源采集统一调度管道** — `worker.collect_all` 升级为配置驱动的源注册表（`collector.sources`：rss / web / vault），单一调度入口跑全部启用源。
  - **Vault (Obsidian) 采集器** — `collectors/vault.py`：扫描 markdown 笔记库转 CollectResult；排除隐藏目录（.obsidian/.git/.trash/.smart-env）与 template.md；幂等键 `vault:<relpath>:<mtime>`（改过的笔记以新版本再采，未动过的去重跳过）。
  - **配置驱动源注册表** — `load_source_registry`；无配置回落旧版 RSS-only 行为（存量部署零影响）；未知源类型抛 ValueError，绝不静默跳过；单源失败隔离进 `results["errors"]` 不中断其他源。
  - **幂等键统一** — RSS `rss:sha256(url|title)`、web `web:sha256(url)`、vault `vault:relpath:mtime`，逐条入库防重。
  - Web 采集错误从 `collect_url` 的静默吞没中上浮到 `results["errors"]`。
  - 8 项新测试（tests/test_p19_ingestion_pipeline.py）。

### Changed · 变更
- **版本号 12.0.2 → 12.1.0**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml`）。
- （master 先行合入）动态 agent 注册表 d91b0fc——v12.1.0 任务1。

---

## v12.0.2 — 2026-08-18 · 安全与隔离修复 (Security Hardening)

### Security · 安全修复
- **ACL 联邦隔离 (High)**：修复 `store.can_access` 中对所有已认证主体无条件注入 `federated_agents` 角色导致 `shared` 可见性塌陷为 `all` 的缺陷。
- **特权分离 (High)**：移除 `learning.py` 中所有硬编码的 `actor_principal != "sandro"` 特权绕过分支，统一由系统鉴权层控制。
- **越权修复 (Med->High)**：修复 `GET /v10/opinions` 与 `GET /v10/observations` 列表端点未按调用主体做 IDOR 隔离的问题。
- **符号记忆租户隔离 (Med)**：在 `symbolic_blocks` 和 `symbolic_canvases` 表中加入 `owner_principal` 字段并建立索引，API 层强制租户鉴权。
- **Web 采集器 SSRF 防御 (Low)**：为 `WebCollector` 增加协议限制与私有/回环/保留 IP 阻断检查。
- **实体解耦 (Low)**：将 `relevance.py` 中硬编码的私有拓扑与 Agent 名单解耦为通用领域术语。

---

## v12.0.1 — 2026-08-16 · 读侧打通 (Hermes MemoryProvider live)

### Added · 新增
- **Hermes MemoryProvider flat-layout plugin** — `hermes-plugin/mimir_memory_provider/`。
  实现 `agent.memory_provider.MemoryProvider` ABC：`prefetch()` 每轮召回注入，
  `get_tool_schemas()`/`handle_tool_call()` 暴露 4 个工具。写路径保持
  mimir-v9.2-cdc 不变（避免重复抽取）。
- **`/v8/query` 审计** — `execute_query` 每次成功检索写 `audit_log(action='query')`，
  使 `/v12/evolve/report` 的 `audit_query_count` 有真实数据。
- **install.sh 重写** — 安装目录布局 + 配置提示。

### Fixed · 修复
- **插件 search 请求体** — 全部三处 `{"query": ...}` → `{"text": ...}`，修复
  `/v8/query` 422 静默失败。
- **Hermes healthcheck** — gateway 检查从 system scope 改为 user scope。
- **README** — schema 徽章 14 → 18；Roadmap 表更新。

### Deployed · 部署
- `memory.provider: mimir_memory_provider` + `plugins.enabled` 配置更新。
- 4 个 gateway + mimir.service 已重启，查询流与首条 useful 反馈已验证入库。

---

## v12.0.0 (schema 18) — 2026-08-14 · 代号 Insight (借鉴 aiduMEI v18.3)

### Added · 新增
- **M1a Ebbinghaus 三轨遗忘** — `DECAY_TIER_MAP` 细化，新增 `L5_ephemeral`
  （半衰期 7d），降权不删行。
- **M1b Chronos 双时间轴** — facts 增加 `valid_from/valid_to`；过期降权 50%。
- **M1c EvolveMem** — `search_feedback` + `quality_metrics` 表；systemd
  `mimir-v12-evolve` 每 6h 聚合，有用 +0.05 / 无用 -0.05。
- **M1d Hermes MemoryProvider 插件** — 钩子 on_turn_start/on_turn_end/
  before_context_compress/on_memory_update。
- **M2a 召回漏斗** — `QueryKernel.trace()` 五阶段；`POST /v12/search/trace`。
- **M2b Dashboard** — 新增「检索」tab：漏斗可视化 + 质量看板。
- **M3a 冲突消解** — `conflict_resolutions`（schema 16），败方置 disputed 不删行。
- **M3b 技能结晶** — `crystal_candidates`（schema 17），7 天 topic 聚类 ≥3 → 候选，
  人工 approve 才结晶。
- **M3c MCP 扩展** — 27 个工具。
- **M4 包装** — PyPI、Dockerfile 自包含、Obsidian Wikilink 双链、多模态（schema 18）。

### Changed · 变更
- **Version**: 11.0.0 (schema 14) → 12.0.0 (schema 18)
- Tests: 113 → 198 passed + 23 subtests。

---

## v11.0.0 (schema 14) — 2026-08 · 全量升级 (借鉴 TencentDB Agent Memory)

> **版本史说明**：v9~v11 时代早于开源仓——公开 git 历史起点是 v12.0.0 开源首发
> （`92c41b7`，2026-08-18）。v9 仅存发布候选快照（`9.0.0-rc1~rc3`），v10/v11 的
> 内部 git 树未随开源导入（内网时代代码含已被开源前清洗的机密面，重新公开
> 属安全倒退）。本节及以下由 CHANGELOG 文本承载历史；对应 tag 自 v12.0.0 起。

### Added · 新增
- **Symbolic short-term memory**（`symbolic_memory.py`）— Mermaid canvas 卸载引擎。
- **CodeGraph**（`code_symbols`, `code_relations`）— 代码符号索引、调用图、影响分析。
- **v11 API**：`/v11/symbolic/*`, `/v11/code/*`。
- **`/v10/reflect/{topic}`** — 从相关 facts + opinions 合成洞见。
- **`/v10/federation/{peer_hierarchy:path}`** — 跨主体的 ACL 共享搜索。
- **Dashboard**：2 个新 tab（符号、代码）+ Claude 风格重设计。

### Changed · 变更
- **Version**: 10.1.0 → 11.0.0, Schema 13 → 14
- Dashboard frontend 全量重写（CSS 变量、明暗切换、移动端底导航）。

---

## v10.0.0 (schema 13) — 2026-08-11

### Added · 新增
- **Governance pipeline** 进入包内（`mimir_v8/governance.py`）
- **Opinion confidence evolution** — set_opinion 端点 + evolve ±0.1 调整
- **Opinion consolidation** — ≥3 同主题高置信生成 observations
- **fast_track auto-commit** — 审核通过的 provisional 候选经 HTTP 提交
- **Dashboard 代理端点**：/api/opinions, /api/observations, /api/governance/decisions

### Changed · 变更
- QueryKernel 增加 `include_provisional` 参数
- systemd units 从 v9.3.0 → v10.0.0 升级

### Fixed · 修复
- 缓存 key 死键累积、Dashboard 写操作 Bearer token 鉴权、opinions 分组查询

---

## v9.x — 2026-08

- v9.3.0 (schema 12)：全量 v9.0 发布，搜索预览、事实全状态查询
- v9.0.0 (schema 12)：嵌入 → 规范化存储 + 投影 + 首次审计
- v8.1.0 (schema 11)：候选管线「filter-confidence」路径、Jaccard 去重
- v8.0.0：初始事件溯源 API 结构
