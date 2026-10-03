# 冻结评测协议（1.2.0 差距#1）

本文件是**跑分纪律的载体**，不是说明文档。凡与本文件冲突的数字，一律作废。

## 一、规矩三条

1. **口径随数字同行。** 同一个 `hit_rate@3`，在 FTS-only 与全通道下不是同一个数。
   每组数字必须带 `engine` 字段（见下），缺字段的数字不得引用、不得发布。
2. **说过的话到期就要兑现。** 承诺过的数字必须真跑出来；跑不出来就明说「未跑」，
   不拿「预计」「应该」占位。**坏消息照样公布**——指标下降也是结果。
3. **连错题本一起交。** 逐案例面（`cases[]`）必须随汇总数字一同保留，
   至少保留到能复现该数字为止。不给汇总数字挑软柿子（sentinel 不挑软柿子）。

## 二、数据集纪律

**外部数据集不进仓**（许可面）。仓内只留**指纹**：

- `benchmarks/data_manifest.json` —— 逐样本 `sha256` + 会话/问题计数 + 来源 URL
- 生成方式：`scripts/verify_benchmark_data.py`（可复算，不需要网络）

`data_manifest.json` 里的 `sha256` 与 `sample_sha256` 是**版本锁**：
换了数据集版本必须重新生成 manifest，并在 RESULTS.md 里新增一条编号记录，
**不得原地覆盖旧编号的数字**。

## 三、跑分入口

```
# 金标腿（打活服务，语料在仓内，无外部依赖）
python scripts/run_benchmarks.py --golden --out report.json

# 外部公开基准（一次性本地库，绝不往生产灌基准语料）
python scripts/run_benchmarks.py --locomo data/locomo10.json --out report.json
```

引擎差异必须如实标注：

| 腿 | engine | 说明 |
|---|---|---|
| `--golden` | 全通道（live `/v8/query` 标准装配层） | 生产真实质量哨兵 |
| `--locomo` / `--longmemeval` | `fts-only trigram + RRF (no vector lane)` | 一次性本地库，无向量挡位 |

## 四、计分语义（`mimir_v8/benchmarks_external.py`）

- `query(question)` → top-k `session_key` 列表
- `hit@K`：top-K 内出现任一 golden key
- `recall@K`：`|top-K ∩ golden| / |golden|`（multi-session 案例才有信息量）
- `mrr`：首个命中的 rank 倒数
- **abstention 案例（无 evidence）显式剔除并计数**，不静默计分
- **query 抛错 → 该案例标 `degraded`，不计入分母**（degraded ≠ miss）
- **全错/全剔除时指标段输出 `None`，不输出零冒充「测过」**

## 五、floor 门禁

金标腿受 `mimir_v8/eval_suite.py::GOLDEN_FLOORS` 约束
（当前 `hit_rate@3 ≥ 0.65`、`hit_rate@10 ≥ 0.83`）。
**floor 被突破时 `run_benchmarks.py` 以非零码退出**，供流水线阻断——
非零退出是设计，不是失败，别在流水线里把它吞成警告。

## 六、可复现定义

同一 commit + 同一数据集（manifest sha256 相同）+ 同一 engine → 同一数字。
报告 JSON 须含 `mimir_version` / `schema_version` / `run_at`，
三者与 RESULTS.md 的记录一致才算数。