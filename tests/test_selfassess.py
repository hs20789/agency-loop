"""agency_loop.flow.selfassess: validation, tier, axis comparison, next difficulty (ARCHITECTURE §5.6)."""

import json
import re
import unittest

from agency_loop.flow.selfassess import compare_axes, next_difficulty, tier_for, validate


def sa(u, v, c):
    return {"understanding": u, "verification": v, "control": c}


def axes(u, v, c):
    return {"understanding": u, "verification": v, "control": c}


class ValidateTest(unittest.TestCase):
    def test_accepts_three_int_axes(self):
        self.assertEqual(validate(sa(0, 1, 2)), sa(0, 1, 2))

    def test_rejects_everything_else(self):
        bad = [
            None, [], "0,1,2", {}, {"understanding": 1, "verification": 1},
            {**sa(1, 1, 1), "extra": 1}, sa(3, 1, 1), sa(-1, 1, 1), sa(True, 1, 1), sa(1.0, 1, 1), sa("1", 1, 1),
            sa(None, 1, 1),
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)


class TierTest(unittest.TestCase):
    def test_boundaries(self):
        cases = {
            (0, 0, 0): "basic", (1, 1, 0): "basic", (2, 0, 0): "basic",
            (1, 1, 1): "standard", (2, 1, 1): "standard", (2, 2, 0): "standard",
            (2, 2, 1): "advanced", (2, 2, 2): "advanced",
        }
        for values, tier in cases.items():
            with self.subTest(values=values):
                self.assertEqual(tier_for(sa(*values)), tier)


class CompareAxesTest(unittest.TestCase):
    def test_relations_and_labels(self):
        rows = compare_axes(sa(2, 1, 0), axes("partial", "partial", "shown"))
        self.assertEqual([r["axis"] for r in rows], ["understanding", "verification", "control"])
        self.assertEqual([r["relation"] for r in rows], ["higher", "similar", "lower"])
        first = rows[0]
        self.assertEqual(
            (first["axis_label"], first["self_label"], first["observed"], first["observed_label"]),
            ("이해", "할 수 있다", "partial", "일부"),
        )
        self.assertTrue(first["message"].startswith("이해: "))

    def test_never_a_score(self):
        blob = json.dumps(compare_axes(sa(0, 1, 2), axes("not_shown", "shown", "partial")), ensure_ascii=False)
        for word in ("점수", "정답", "score", "%", "능력이"):
            self.assertNotIn(word, blob)
        self.assertIsNone(re.search(r"\d+\s*점", blob))


class NextDifficultyTest(unittest.TestCase):
    def test_rules(self):
        all_shown = axes("shown", "shown", "shown")
        one_missing = axes("shown", "not_shown", "partial")
        mixed = axes("shown", "partial", "partial")
        cases = [
            ("basic", all_shown, "harder"), ("standard", all_shown, "harder"), ("advanced", all_shown, "same"),
            ("advanced", one_missing, "easier"), ("standard", one_missing, "easier"), ("basic", one_missing, "same"),
            ("basic", mixed, "same"), ("standard", mixed, "same"), ("advanced", mixed, "same"),
        ]
        for tier, observed, code in cases:
            with self.subTest(tier=tier, observed=observed):
                result = next_difficulty(tier, observed)
                self.assertEqual(result["code"], code)
                self.assertEqual(set(result), {"code", "label", "message"})
                self.assertTrue(result["label"] and result["message"])


if __name__ == "__main__":
    unittest.main()
