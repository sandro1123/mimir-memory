# -*- coding: utf-8 -*-
"""时序数据面（loader 时间戳解析）· 1.2.0 卡二 GREEN-1 —— 已交付。

背景：本卡原计划做「时序检索道」（temporal lane）。施工前的值域枚举把靶子
证伪了——LoCoMo cat2（时序类）320 条里只有 **40 条（12.5%）**带可寻址的
时间表达，85.9% 是裸 `When did X …?`，问题里根本没有时间信息可读。
**车道作废**，见 `docs/plans/1.2.0-card2-temporal-lane.md` §六。

**但 loader 侧这一半保留并交付**，理由与车道无关：时间戳此前**根本进不了库**
（`load_locomo` 从不读 `session_N_date_time`，每条 session 的 `valid_from`
全空）。现在解析率 272/272 = 1.0，后续任何要读时间的活儿（衰减、冲突消解、
时间线视图）都从这一层起，不必回头补数据。

三条钉守的是**解析器的值域**，不是车道：
1. 真实 LoCoMo 格式（`'1:56 pm on 8 May, 2023'`）必须解析成功——它不是 ISO，
   `fromisoformat` 解析不了；
2. 解析不出来必须计数留痕、session 照常入库（丢整批会让检索面静默变窄）；
3. **解析不了的时间表达绝不猜成日期**——猜出来的日期会进排序且没人看得见
   它被猜了，比不参与更坏。这条是本卡的核心守卫。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mimir_v8.benchmarks_external import (  # noqa: E402
    load_locomo, load_longmemeval, BenchmarkCorpus, SessionRecord,
    parse_locomo_date)


class TestTemporalData(unittest.TestCase):
    # ── 正向 ────────────────────────────────────────────────
    def test_locomo_dates_are_parsed_not_discarded(self):
        """loader 必须把 session_N_date_time 解析进 SessionRecord.occurred_at。

        恒等式两条数：解析成功 + 解析失败 == 总 session 数（不得有第四种去向）。
        """
        sample = {
            "sample_id": "t0",
            "conversation": {
                "speaker_a": "A",
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_1": [{"speaker": "A", "text": "hi", "dia_id": "D1:0"}],
                "session_2_date_time": "NOT A DATE AT ALL",
                "session_2": [{"speaker": "A", "text": "yo", "dia_id": "D2:0"}],
            },
            "qa": [{"question": "q", "category": 1, "evidence": ["D1:0"]}],
        }
        corpus = load_locomo([sample])
        parsed = [s for s in corpus.sessions if s.occurred_at]
        self.assertTrue(parsed, "真实 LoCoMo 日期格式必须被解析成功")
        self.assertEqual(parsed[0].occurred_at, "2023-05-08")
        # 脏日期：计入失败数，但 session 照常入库（不得整批丢弃）
        self.assertEqual(len(corpus.sessions), 2)
        self.assertEqual(corpus.n_sessions_without_date, 1)

    def test_parse_rate_is_a_reported_number_not_an_assumption(self):
        """解析率必须能算出来——它是结论的一部分，不是「应该有」。

        四条会话：两条真日期、一条纯 ISO、一条脏串。
        """
        sample = {
            "sample_id": "t2",
            "conversation": {
                "speaker_a": "A",
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_1": [{"speaker": "A", "text": "a", "dia_id": "D1:0"}],
                "session_2_date_time": "2023-06-01",
                "session_2": [{"speaker": "A", "text": "b", "dia_id": "D2:0"}],
                "session_3_date_time": "12:00 am on 1 January, 2024",
                "session_3": [{"speaker": "A", "text": "c", "dia_id": "D3:0"}],
                "session_4_date_time": "sometime last spring",
                "session_4": [{"speaker": "A", "text": "d", "dia_id": "D4:0"}],
            },
            "qa": [{"question": "q", "category": 1, "evidence": ["D1:0"]}],
        }
        corpus = load_locomo([sample])
        self.assertEqual(len(corpus.sessions), 4)
        self.assertEqual(corpus.n_sessions_without_date, 1)
        dated = {s.session_key.rsplit(":", 1)[1]: s.occurred_at
                 for s in corpus.sessions}
        self.assertEqual(dated["session_1"], "2023-05-08")
        self.assertEqual(dated["session_2"], "2023-06-01")
        self.assertEqual(dated["session_3"], "2024-01-01")
        self.assertIsNone(dated["session_4"])

    def test_legacy_shapes_still_build(self):
        """回归守：SessionRecord 三参构造仍可用（occurred_at 有默认值）。

        加了字段的老调用方不能被这次改动打死——四参是新的，三参必须还活着。
        """
        record = SessionRecord(session_key="k", owner="o", text="t")
        self.assertIsNone(record.occurred_at)
        corpus = BenchmarkCorpus(benchmark="locomo")
        self.assertEqual(corpus.n_sessions_without_date, 0)

    # ── 边界 ────────────────────────────────────────────────
    def test_unparseable_date_counts_but_keeps_session(self):
        sample = {
            "sample_id": "t1",
            "conversation": {
                "speaker_a": "A",
                "session_1_date_time": "sometime last spring",
                "session_1": [{"speaker": "A", "text": "hi", "dia_id": "D1:0"}],
            },
            "qa": [{"question": "q", "category": 1, "evidence": ["D1:0"]}],
        }
        corpus = load_locomo([sample])
        self.assertEqual(len(corpus.sessions), 1)
        self.assertIsNone(corpus.sessions[0].occurred_at)
        self.assertEqual(corpus.n_sessions_without_date, 1)

    def test_session_without_date_field_is_counted_not_guessed(self):
        """源里压根没有 _date_time 字段 → 同样计数。

        「字段不存在」与「字段存在但解析不出」必须落进同一个计数——
        否则解析率会虚高：读不到的时间不算漏，那报告就在说谎。
        """
        sample = {
            "sample_id": "t3",
            "conversation": {
                "speaker_a": "A",
                "session_1": [{"speaker": "A", "text": "hi", "dia_id": "D1:0"}],
            },
            "qa": [{"question": "q", "category": 1, "evidence": ["D1:0"]}],
        }
        corpus = load_locomo([sample])
        self.assertIsNone(corpus.sessions[0].occurred_at)
        self.assertEqual(corpus.n_sessions_without_date, 1)

    # ── 反向（本卡核心守卫）──────────────────────────────────
    def test_relative_time_expression_is_never_guessed(self):
        """核心守卫："next tuesday" 一类表达一律 None，绝不猜。

        猜一个日期去参与排序，比不参与更坏：没人看得见它被猜了。
        """
        for phrase in ("next tuesday", "in a while", "sometime soon",
                       "shortly after lunch", "8 May"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(parse_locomo_date(phrase))

    def test_illegal_calendar_date_is_rejected_not_corrected(self):
        """2023-02-30 判非法，不「修正」成 2 月 28 日。

        悄悄修正 = 悄悄猜。同族：月份名不认识、缺年份，一律 None。
        """
        for bad in ("2023-02-30", "30 February, 2023", "8 Foo, 2023",
                    "", None, 123, []):
            with self.subTest(value=bad):
                self.assertIsNone(parse_locomo_date(bad))

    def test_valid_calendar_dates_round_trip(self):
        """正向值域：合法输入必须真解析出来（守卫不能只测负向）。"""
        cases = {
            "1:56 pm on 8 May, 2023": "2023-05-08",
            "8 May, 2023": "2023-05-08",
            "7:03 am on 1 January, 2024": "2024-01-01",
            "12:30 am on 15 December, 2022": "2022-12-15",
            "2023-05-08": "2023-05-08",
            "11:45 pm on 28 February, 2020": "2020-02-28",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_locomo_date(raw), want)


if __name__ == "__main__":
    unittest.main()