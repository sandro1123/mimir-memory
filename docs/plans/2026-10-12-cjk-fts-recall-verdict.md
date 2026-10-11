# #26 CJK/FTS 召回率实测 —— 否决结论（无需补丁）

> 2026-10-12 · roadmap #26 · `kind:research`
> **纪律：先量后写，不照抄。** 若 Mímir 召回率已达标，本卡的正确产出是**否决结论**，不是补丁。

## 问题

claude-mem 报告：未分词文字（CJK 汉字 / 平假名 / 片假名 / 注音 / 谚文）在 FTS5
`unicode61` tokenizer 下折叠成单 token，导致子串检索完全落空
（其 `SessionSearch.ts:288-330,467`）：

- `설정` 检 `설정을` → **0 行**
- `ㄓㄨ` 检 `ㄓㄨㄛ` → **0 行**
- `项目` 检 `修改了项目配置` → **0 行**

它的补法是 `UNSEGMENTED_SCRIPT.test(query)` → `searchObservationsBySubstring`。

## 测量方法

直接调 `mimir_v8.projector.FTSProjector.search_ids()` **真实代码路径**，不绕行、不 mock：
建 6 条含 CJK/韩文/日文/注音的文档，用 12 个 query 对照期望命中。

测量脚本与逐例输出留存在工作区（`measure_cjk.py` / `measure_result.txt`），未入仓。

## 结果：12/12 命中

| query | expect | 说明 |
|---|---|---|
| `설정` | doc-ko1 | 韩语短词 |
| `설정을` | doc-ko1 | 韩语完整词 |
| `ㄓㄨ` | doc-bopo1 | 注音单字 |
| `ㄓㄨㄛ` | doc-bopo1 | 注音长串 |
| `项目` | doc-zh1 | **claude-mem 失败例** |
| `项目配置` | doc-zh1 | 4 字中文词 |
| `修改了项目配置` | doc-zh1 | 长串原文 |
| `プロジェクト` | doc-ja1 | 片假名 |
| `設定` | doc-ja1 | kanji in katakana+kanji |
| `召回漏斗` | doc-zh2 | 混合文档 |
| `recall funnel` | doc-en1 | 英文控制组 |
| `degraded` | doc-en1 | 英文单词 |

## 根因：两条独立路径分别兜住短查询与长查询

**先纠正一个我差点写错的归因**：初稿把 12/12 全归功于 trigram。反向验证
（把 `tokenize` 改回 `unicode61` 再逐条探针）证明**并非如此**——两条路径各管
一段，缺任何一条都会破：

| query | 长度 | 走的路径 | unicode61 下 |
|---|---|---|---|
| `설정` / `ㄓㄨ` / `项目` / `設定` | 2 | **短 token LIKE 兜底**（`<3` 字符） | **仍命中** |
| `项目配置` / `プロジェクト` | 4 | MATCH 直接 | **落空** |
| `修改了项目配置` | 7 | 长 CJK trigram 展开 | **落空** |

即：**claude-mem 报的三个失败例（全是 2 字符）在 Mímir 这里是由 LIKE 兜底救的，
不是 trigram；trigram 真正守护的是 4 字符以上的长查询。**

`mimir_v8/projector.py` 的两层（`92c41b7` 起即如此，非后补）：

1. `tokenize='trigram'`（第 40 行）+ `_ensure_trigram_index()` 自动重建
   （第 56-84 行）——支撑 **4 字符以上**的 CJK 子串，以及长 CJK 词的重叠
   trigram 片段展开（第 181-187 行）；
2. `search_ids()` 对 `<3` 字符 token **回退 LIKE 子串**（第 202-214 行）——
   trigram 无法匹配 3 字符以下的输入，这一段由 LIKE 独立兜住。

**这个更正有实际后果**：它意味着回归测试必须同时钉住两条路径，
只断言 trigram 会漏掉一半。见 `tests/test_p26_cjk_substring_recall.py`。

## 判定

**否决补丁。** 召回率达标，无需复现 claude-mem 的子串兜底方案。

**遗留观察（非本卡范围）**：现有测试集中没有专门针对 CJK 的回归用例——
`test_p02/p24/p27/p47` 等只把 `FTSProjector` 当夹具。若日后有人改动
`search_ids()` 或 tokenizer，缺少一道直接守护"claude-mem 三失败例"的回归网。
建议另立小额卡（不阻断本卡关闭）。
