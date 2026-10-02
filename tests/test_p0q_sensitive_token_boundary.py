"""P0-Q 判例「有规则≠规则对」— governance.deterministic_check `token` 边界修复.

生产事故 10-02: 42 human_review 里 20 条是「每百万 token / $x per token」
定价语境被 SENSITIVE_PATTERNS 裸 `token` 短路标 risk=high，根本没过 LLM。
中文 + 英文都犯。修: token 只在确实命中**凭据形态**时拦，不拦计量单位语境。
"""
from __future__ import annotations

import unittest

from mimir_v8.governance import deterministic_check

# 定价/用量语境——这些「token」只是计量单位，不该被 accelerometer 拦下
PRICING_CONTEXT = (
    "OpenAI GPT-6 Sol 定价 $0.10/1M tokens，输出 $0.50/1M tokens",
    "Claude Opus 5.5 价格降至输入 $4/输出 $20 每 1M tokens",
    "GPT-6 Sol (Max) 榜 1689 分，价格 $8/M tokens",
    "Kimi K2.5 成本减少 40% token，降本 86%",
    "OpenAI GPT-6 Sol priced at $0.10 per 1M tokens",
    "reduce LLM token consumption by 40%",
)

# 凭据/凭据语境——真该拦
CREDENTIAL_CONTEXT = (
    "config token = abc123def456",
    "API key = [redacted]",
    "access_token: xyz789",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
    "从日志提取带 token 的访问 URL",
)


class TestTokenPricingContextNotSensitive(unittest.TestCase):
    def test_pricing_across_languages_not_flagged(self):
        for s in PRICING_CONTEXT:
            with self.subTest(s=s):
                self.assertEqual(
                    deterministic_check(s)["matched_patterns"], [],
                    f"定价语境被误拦: {s!r}",
                )

    def test_credential_still_flagged(self):
        for s in CREDENTIAL_CONTEXT:
            with self.subTest(s=s):
                self.assertNotEqual(
                    deterministic_check(s)["matched_patterns"], [],
                    f"凭据语境没被拦: {s!r}",
                )


if __name__ == "__main__":
    unittest.main()
