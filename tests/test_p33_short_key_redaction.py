# -*- coding: utf-8 -*-
"""Issue #33: the DLP rule missed short keys because of a length floor.

The 2026-11-01 scan found five facts holding key material and the rule only
flagged two of them: ``sk-4166fcd0`` (8-character body) sat below the
``{16,}`` floor, so a real key read as prose. The floor exists to stop
``sk-learn``-style words from being redacted, so it stays for the general
alphanumeric form — but a hex body of eight or more characters is a key shape,
not a word.
"""
import unittest

from mimir_v8.learning import redact_text
from mimir_v8.vault_inventory import CREDENTIAL_PATTERNS


class TestShortKeyRedaction(unittest.TestCase):
    def test_short_hex_key_is_redacted(self):
        result = redact_text("配置里写过 sk-4166fcd0 这个 key")
        self.assertTrue(result.redacted)
        self.assertNotIn("sk-4166fcd0", result.text)

    def test_assignment_form_is_redacted(self):
        result = redact_text("api_key=sk-4166fcd0")
        self.assertTrue(result.redacted)
        self.assertNotIn("sk-4166fcd0", result.text)

    def test_named_key_is_redacted(self):
        result = redact_text("本地密钥 sk-odin-zen-gateway")
        self.assertTrue(result.redacted)
        self.assertNotIn("sk-odin-zen-gateway", result.text)

    def test_long_general_form_still_redacted(self):
        result = redact_text("sk-proj-xxxxxxxxxxxxxxxx")
        self.assertTrue(result.redacted)

    def test_prose_that_merely_starts_with_sk_is_untouched(self):
        """Guarding the guard: over-redaction makes the rule useless."""
        for text in ("use sk-learn for ML", "the sk-image tag", "a sk-"):
            result = redact_text(text)
            self.assertFalse(result.redacted, text)
            self.assertEqual(result.text, text)


class TestVaultScannerParity(unittest.TestCase):
    """The inventory scanner and the DLP rule must flag the same shapes."""

    SAMPLES = (
        "sk-4166fcd0",
        "sk-odin-zen-gateway",
        "sk-proj-xxxxxxxxxxxxxxxx",
        "api_key=sk-4166fcd0",
    )

    def test_scanner_flags_every_short_key(self):
        for sample in self.SAMPLES:
            self.assertTrue(
                any(pattern.search(sample) for pattern in CREDENTIAL_PATTERNS),
                f"scanner missed {sample!r}",
            )

    def test_scanner_ignores_ordinary_prose(self):
        for text in ("use sk-learn for ML", "the sk-image tag"):
            self.assertFalse(
                any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS), text
            )


if __name__ == "__main__":
    unittest.main()
