"""Exhaustive small candidates and bounded quantization regressions."""

import itertools
import random
import unittest

from oea.context.selection import Group, select_views, utility
from oea.context.views import reference_message, tool_reference
from oea.contracts.measurement import estimate_input, measurement


class SelectionTests(unittest.TestCase):
    def test_exact_small_choices_match_exhaustive_search(self):
        rng = random.Random(29)
        for _ in range(120):
            groups = [Group(rng.randint(2, 12), rng.randint(1, 7), rng.choice((False, False, True))) for _ in range(rng.randint(1, 5))]
            budget = rng.randint(0, 35)
            for allow_summary in (False, True):
                solutions = []
                for modes in itertools.product(("full", "reference", "summary"), repeat=len(groups)):
                    prefix = sum(mode == "summary" for mode in modes)
                    if prefix and (not allow_summary or prefix == len(groups) or modes[:prefix] != ("summary",) * prefix or any(group.protected for group in groups[:prefix])):
                        continue
                    if any(mode == "reference" and group.reference >= group.full for group, mode in zip(groups, modes)):
                        continue
                    cost = sum(group.full if mode == "full" else group.reference if mode == "reference" else 0 for group, mode in zip(groups, modes))
                    if cost <= budget:
                        score = sum(utility(i, len(groups), mode, 2) for i, mode in enumerate(modes) if mode != "summary")
                        solutions.append((prefix, score, cost))
                if any(prefix == 0 for prefix, _, _ in solutions):
                    solutions = [item for item in solutions if item[0] == 0]
                result = select_views(groups, budget, recent_turns=2, allow_summary=allow_summary)
                if not solutions:
                    self.assertIsNone(result)
                    continue
                expected = max(solutions, key=lambda item: (item[1], -item[2]))
                self.assertEqual((result.utility, result.cost), expected[1:])
                self.assertLessEqual(result.cost, budget)
                self.assertEqual(result.modes[:result.summarized], ("summary",) * result.summarized)
                self.assertNotIn("summary", result.modes[result.summarized:])
                self.assertEqual(result, select_views(groups, budget, recent_turns=2, allow_summary=allow_summary))

    def test_quantization_failure_uses_exact_minimum_without_summary(self):
        groups = [Group(6000, 3906) for _ in range(256)]
        result = select_views(groups, 1_000_000, allow_summary=True)
        self.assertTrue(result.fallback)
        self.assertEqual(result.summarized, 0)
        self.assertEqual(result.cost, 999936)
        self.assertLessEqual(result.capacity, 4096)

    def test_latest_and_unresolved_groups_cannot_be_summarized(self):
        self.assertIsNone(select_views([Group(200, 100, True), Group(200, 100)], 100, allow_summary=True))
        self.assertIsNone(select_views([Group(200)], 100, allow_summary=True))

    def test_reference_preserves_calls_arguments_and_terminal_facts(self):
        message = {"role": "assistant", "content": "explanation " * 100, "tool_calls": [{"id": "c", "function": {"name": "read", "arguments": '{"path":"source"}'}}]}
        view = reference_message(message, {"id": "entry", "position": 3})
        self.assertEqual(view["tool_calls"], message["tool_calls"])
        value = {"stdout": "x" * 5000, "exit_code": 7, "isError": True, "error": {"code": "FAILED"}, "unresolved_actions": ["a"], "content_view": {"integrity": "truncated", "source_complete": False}}
        reference = tool_reference(value, "source")
        self.assertEqual(reference["exit_code"], 7)
        self.assertEqual(reference["error"], value["error"])
        self.assertEqual(reference["unresolved_actions"], ["a"])
        self.assertFalse(reference["content_view"]["source_complete"])
        self.assertNotIn("stdout", reference)

    def test_image_encoding_does_not_change_the_input_estimate(self):
        logical = [{"role": "user", "content": [{"type": "oea_image", "artifact_id": "image"}]}]
        wire = [{"role": "user", "content": [{"type": "text", "text": "Assigned image evidence: image"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 100000, "detail": "auto"}}]}]
        self.assertEqual(estimate_input(logical, []), estimate_input(wire, []))
        self.assertEqual(estimate_input(logical, [])["basis"], "heuristic_estimate")
        self.assertEqual(measurement("limit", 2, "token", "unknown", limit_source="unknown")["decision"], "unknown")
