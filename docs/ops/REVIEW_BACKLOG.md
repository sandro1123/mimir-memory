# 人工审核积压（review-backlog）

1.9.1 起。处理治理转入 `human_review` 的候选。`list` 只读，`apply` 默认演练。

## 运行方式

与其他 worker 子命令相同，在生产 release 目录下用生产 venv 运行（数据目录与
`mimir.service` 一致，由 worker 自身的配置定位）：

```bash
cd ~/.hermes/mimir/releases/current
PYTHONPATH=. ~/.hermes/mimir/venvs/current/bin/python3 -m mimir_v8.worker review-backlog list
```

## 查看

```bash
review-backlog list                          # 汇总 + 最老的 50 条
review-backlog list --bridge-noise           # 只看原始桥接元数据
review-backlog list --owner odin --older-than 7d
review-backlog list --reason-code duplicate_gate --json
```

原因码：`duplicate_gate`（判重拦截）、`high_risk`、`llm_unavailable`、
`low_confidence_noise`（LLM 判无价值但置信度低）、`needs_human_confirmation`、`other`、`unspecified`。原因取候选自身的 `review_reason`，为空时取最近一条治理决定的 reason。

## 批量处理

```bash
# 先演练（不写库），确认清单
review-backlog apply --bridge-noise --action reject --reason "原始桥接元数据，非用户内容" --actor admin
# 确认无误后加 --execute
review-backlog apply --bridge-noise --action reject --reason "原始桥接元数据，非用户内容" --actor admin --execute
# 按 id
review-backlog apply --ids <id1>,<id2> --action approve --reason "人工确认" --actor admin --execute
```

- 必须给 `--reason` 和审阅人（`--actor` 或 `MIMIR_REVIEW_ACTOR`）。审阅人应是人，
  `service:*` 批准不会把事实标为 confirmed。
- 不带 `--ids` 又没有任何筛选时拒绝执行，防止误清整个积压。
- approve 会立即 commit 成事实；reject 只改候选状态。都写 `memory_events` 与审计。
- 单条失败不影响其余；有任何失败 exit 1。重复执行同一批：已处理的不在积压里，会报失败而不是重复写入。
- confidential / restricted 候选的预览一律遮蔽，其余经 `redact_text`。

## 生产注意

执行 `--execute` 前先备份 `canonical.db`，并在演练输出里逐条确认。
