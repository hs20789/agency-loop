"""agency_loop.flow.compare: grouping by cited evidence ids only (ARCHITECTURE §5.5)."""

import json
import unittest

from agency_loop.flow.compare import NOTE, compare


def graded(text, ids, invalid=()):
    return {"text": text, "evidence_ids": list(ids), "invalid_ids": list(invalid), "grade": "inferred"}


class CompareTest(unittest.TestCase):
    def test_groups_by_valid_cited_ids(self):
        a1, a2, a3 = graded("a1", ["E1", "E2"]), graded("a2", []), graded("a3", ["E3", "E9"], invalid=["E9"])
        h1, h2 = graded("h1", ["E1"]), graded("h2", ["E9"], invalid=["E9"])
        d1, d2 = graded("d1", ["E2", "E2"]), graded("d2", ["E1"])
        result = compare({"anthropic": [a1, a2, a3], "hcx": [h1, h2], "deepseek": [d1, d2]})

        self.assertEqual(result["same_evidence"], [
            {"evidence_id": "E1", "providers": ["anthropic", "hcx", "deepseek"],
             "statements": {"anthropic": [a1], "hcx": [h1], "deepseek": [d2]}},
            {"evidence_id": "E2", "providers": ["anthropic", "deepseek"], "statements": {"anthropic": [a1], "deepseek": [d1]}},
        ])
        self.assertEqual(result["only_one"], [{"evidence_id": "E3", "providers": ["anthropic"], "statements": {"anthropic": [a3]}}])
        self.assertEqual(result["different"], [{"provider": "anthropic", "statements": [a2]},
                                               {"provider": "hcx", "statements": [h2]}])  # E9 was never a valid id
        self.assertEqual(result["note"], "의견이 같아도 사실로 확정하지 않습니다")
        self.assertEqual(result["labels"]["different"], "비교 불가(유효한 근거 ID 없음)")

    def test_never_a_vote_or_score(self):
        result = compare({"anthropic": [graded("a", ["E1"])], "hcx": [graded("b", ["E1"])]})
        self.assertEqual(set(result), {"same_evidence", "only_one", "different", "labels", "note"})
        blob = json.dumps(result, ensure_ascii=False)
        for word in ("score", "majority", "점수", "다수결", "정답"):
            self.assertNotIn(word, blob)
        self.assertEqual(compare({}), {"same_evidence": [], "only_one": [], "different": [], "labels": result["labels"],
                                       "note": NOTE})


if __name__ == "__main__":
    unittest.main()
