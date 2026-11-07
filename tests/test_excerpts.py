"""Character-safe excerpts obey the shared UTF-8 input estimate."""

import unittest

from oea.context.store import prune_text
from oea.contracts.measurement import estimated_tokens


class ExcerptTests(unittest.TestCase):
    def test_complete_text_does_not_reserve_an_unused_marker(self):
        self.assertEqual(prune_text("ready", 2, "[Read the full source]"), "ready")
        self.assertEqual(prune_text("", 0, "[source]"), "")

    def test_multibyte_ends_and_tiny_budgets_never_exceed_the_estimate(self):
        notice = "\n[original available]\n"
        for unit in ("abc", "汉字", "🙂𐐀", "e\u0301"):
            text = unit * 4096
            for budget in range(65):
                with self.subTest(unit=unit, budget=budget):
                    result = prune_text(text, budget, notice)
                    self.assertLessEqual(estimated_tokens(result), budget)
                    self.assertNotIn("\ufffd", result)
                    if result:
                        prefix, marker, suffix = result.partition(notice)
                        self.assertEqual(marker, notice)
                        self.assertTrue(text.startswith(prefix))
                        self.assertTrue(text.endswith(suffix))

    def test_large_body_keeps_both_ends_and_the_original_unchanged(self):
        original = "HEAD:" + "evidence " * 100000 + ":TAIL"
        result = prune_text(original, 100, "[source]")
        self.assertTrue(result.startswith("HEAD:"))
        self.assertTrue(result.endswith(":TAIL"))
        self.assertEqual(result.count("[source]"), 1)
        self.assertLessEqual(estimated_tokens(result), 100)
        self.assertEqual(len(original), 900010)
