# Claude Code 自动记忆 Hook（事件驱动写入 Mímir）

让远程 Claude Code / cc-connect Agent 的记忆沉淀从「主动调 `mimir-remember`」变成「每轮结束自动判断并写入」。
适用于任何托管 Claude Code 的 Agent 机器（OCI、笔记本、沙箱）。

## 为什么需要它

纯靠提示词要求 Agent「记得写记忆」在实践中不可靠：会忘、会懒、上下文一压缩连「刚才有什么值得记」都想不起来。
Hook 是 Claude Code 自带的生命周期钩子，在模型外部触发，**不依赖模型的自觉**。

## 三个事件的取舍

| 事件 | 时机 | async | 理由 |
|:--|:--|:--|:--|
| `Stop` | 每轮回复结束 | `true` | 常规沉淀，放后台不拖慢响应 |
| `PreCompact` | 上下文压缩**前** | **`false`** | 必须同步：异步跑时代码还没执行，上下文已经被压缩掉了 |
| `SessionEnd` | 会话终止 | `true` | 收尾补漏 |

`PreCompact` 用异步等于没写——这是最容易踩的坑。

## 配置形状（`~/.claude/settings.json`）

```json
{
  "hooks": {
    "Stop":        [{"hooks":[{"type":"command","command":"/home/<user>/.claude/hooks/mimir_auto_hook.py","timeout":120,"async":true}]}],
    "PreCompact":  [{"hooks":[{"type":"command","command":"/home/<user>/.claude/hooks/mimir_auto_hook.py","timeout":120,"async":false}]}],
    "SessionEnd":  [{"hooks":[{"type":"command","command":"/home/<user>/.claude/hooks/mimir_auto_hook.py","timeout":120,"async":true}]}]
  }
}
```

改完必须重启托管服务（`systemctl --user restart feishu-agent` 之类），运行中的进程不会重新读配置。

## Hook 输入的可用字段

- `transcript_path`：会话 JSONL。**异步写入，可能落后于内存中的对话**，拿不到本轮最新内容。
- `last_assistant_message`：`Stop` / `SubagentStop` 事件自带，是本轮 assistant 的最终文本。**优先用它，别只读 transcript。**
- `session_id`：做去重键。
- `hook_event_name`：区分是哪个事件触发。

脚本的 `extract_turn()` 应当 `last_assistant_message` 优先、transcript 只用来补 user 侧文本；两边都空就跳过本轮。

transcript 可能很大，只 seek 尾部若干百 KB 再逐行解析，不要全量载入。

## 判断层的设计取舍

用**便宜模型**做价值判断（`cbcn/glm-5.3-flash` 这类），不要用主模型：

- 判断发生在每一轮结束后，用贵模型成本随对话线性上涨。
- 判断不需要强推理，只需要按规则分类。
- 漏记比滥记代价低——宁可少记。

判断标准（写进 prompt）：值得记 = 用户偏好/规矩、具体配置参数、非显而易见的根因、跨会话有意义的决策。不该记 = 闲聊、中间进度、调试输出、纯代码、一次性问答。

要求模型输出严格 JSON 并做容错解析（剥 ```json 围栏 + 正则截取第一个 `{...}`），小模型常有格式漂移。

## 必须有的护栏

1. **敏感内容正则拦截**：命中 `sk-…`、`ibk_…`、`BEGIN … PRIVATE KEY`、`密码：…` 直接丢弃不写。不能指望上游模型自觉不写。
2. **限流**：每小时最多 N 条（实践值 6），否则长对话会刷屏式写入。
3. **去重**：`session_id + 内容 sha256 前 16 位` 做 seen 键，状态存 `~/.claude/mimir-hook/seen.json`。可设 7 天清理。
4. **单轮条数上限**：最多 2 条。
5. **永不阻塞**：脚本最外层 `try/except` + `sys.exit(0)`。Hook 返回非零可能干扰宿主，宁可静默。
6. **日志落盘**：`~/.claude/mimir-hook/hook.log`，写清「WROTE / judged: nothing / skip 原因 / HTTP 错误」。没有日志等于没法证明它在工作。

## 验证顺序（缺一步都会自欺）

1. **语法**：`python3 -c 'import ast; ast.parse(open(路径).read())'`
2. **干跑**：构造 `{"hook_event_name":"Stop","transcript_path":"<真实会话 jsonl>","session_id":"dryrun"}` 从 stdin 喂给脚本，看日志有没有 `WROTE`。
3. **回读**：拿日志里的 `fact_id` 调 `GET /v8/facts/<fact_id>`，确认 `owner_principal` 与 `content` 正确——**不要用 `/v8/query` 自证**，向量投影有延迟。
4. **端到端**：真跑一轮对话（`claude -p '...' --output-format json`），确认日志里出现 `[Stop] done: wrote N/M`。

只有第 3、4 步都过才算部署成功。

**用 `claude -p` 做端到端测试时，一轮会同时触发 `Stop` 和 `SessionEnd`**——一次性会话在回答结束后立即终止，两个事件都会跑一遍脚本。日志里看到同一时刻两条 `[Stop]` / `[SessionEnd]` 记录是正常的，不是重复触发 bug；真正防住重复写入的是第去重键（`session_id + 内容 hash`）。反过来说，这条测试路径能顺带验证去重是否生效：如果两条事件写出了内容相似的两条事实，说明去重键失效了。

## 与「Agent 主动写入」的关系

两者不冲突，是双保险：

- CLAUDE.md 里的规则 = 主动路径，精准可控，但会忘。
- Hook = 被动兜底，不会忘，但判断靠小模型会漏。

明确重要的事实仍应让 Agent 主动 `mimir-remember`——那条路径不限流、不经小模型、不丢细节。

## 宿主差异

- cc-connect 托管的 Claude Code：hook 由 Claude Code 进程自己触发，与 cc-connect 无关，不需要改 cc-connect 配置。
- 只改 `settings.json` 不改 unit 时，环境变量（`ANTHROPIC_BASE_URL` 等）仍需 unit 里的 `Environment=` 提供，否则脚本调 LLM 判断会失败。
