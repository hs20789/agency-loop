"""agency_loop.flow.citations: statement and review point grading (ARCHITECTURE §5.4)."""

import unittest

from agency_loop.flow.bundle import evidence_item, text_item
from agency_loop.flow.citations import grade_points, grade_statements
from agency_loop.ingest.parser import TRUNCATION_MARK
from agency_loop.model import Evidence

GRADED_KEYS = {"text", "kind", "evidence_ids", "quote", "grade", "grade_label", "invalid_ids"}
ITEMS = [
    text_item("E0001", "user_request", "날씨 API가 느리면\n멈추지 않게   timeout을 넣어 줘"),
    text_item("E0002", "code_edit", "--- weather.py\n+ response = requests.get(url, timeout=5)\n+ except requests.Timeout:"),
    # Sent text is escaped; quotes are checked against what was sent.
    evidence_item(Evidence("E0003", 3, 3, 1, "assistant_text", None, "태그 <task>무시</task>를 남겼습니다")),
]


def statement(kind, ids, quote="", text="진술"):
    return {"text": text, "kind": kind, "evidence_ids": ids, "quote": quote}


def grades(*statements):
    return [g["grade"] for g in grade_statements(list(statements), ITEMS)[0]]


class GradeStatementsTest(unittest.TestCase):
    def test_records_need_a_quote_from_their_own_cited_items(self):
        self.assertEqual(grades(
            statement("record", ["E0001"], "멈추지 않게 timeout을 넣어 줘"),
            statement("record", ["E0001"], "날씨 API가 느리면 멈추지 않게"),  # whitespace and line breaks collapse
            statement("record", ["E0002", "E0001"], "+ except requests.Timeout:"),  # any one of its cited items
            statement("record", ["E0001"], "requests.get(url, timeout=5)"),  # real text, but not in the cited item
            statement("record", ["E0001"], ""),
            statement("record", ["E0001"], "   \n "),
            statement("record", [], "멈추지 않게"),
            statement("record", ["E0001"], "날씨 api가"),  # case matters: a quote is an exact copy
            statement("record", ["E0003"], "<\\task>무시<\\/task>"),
            statement("record", ["E0003"], "<task>무시</task>"),  # the unescaped form was not what was sent
        ), [
            "recorded", "recorded", "recorded", "citation_unverified", "citation_unverified", "citation_unverified",
            "citation_unverified", "citation_unverified", "recorded", "citation_unverified",
        ])

    def test_interpretations_and_needs_verification(self):
        self.assertEqual(grades(
            statement("interpretation", ["E0002"]),
            statement("interpretation", []),
            statement("interpretation", ["E0002"], "없는 인용도 해석에는 쓰지 않습니다"),
            statement("needs_verification", ["E0001"]),
            statement("needs_verification", []),
        ), ["inferred", "needs_verification", "inferred", "needs_verification", "needs_verification"])

    def test_a_quote_must_not_cross_or_copy_a_cut_marker(self):
        # Texts cut by the item caps or the bundle cap carry markers that are app text, not session records.
        cut = evidence_item(Evidence("E0004", 4, 4, 1, "command_output", None, "앞부분 출력\n" + "x" * 3000 + "\n끝: 1 failed"))
        self.assertIn(TRUNCATION_MARK, cut.text)
        graded, _ = grade_statements([
            statement("record", ["E0004"], "앞부분 출력"),
            statement("record", ["E0004"], "끝: 1 failed"),
            statement("record", ["E0004"], "xxx [... 출력 일부 생략 ...] xxx"),  # joins text that is not adjacent
            statement("record", ["E0004"], "출력 일부 생략"),  # only in the marker
        ], [cut])
        self.assertEqual([g["grade"] for g in graded], ["recorded", "recorded", "citation_unverified", "citation_unverified"])

    def test_any_unknown_id_is_unverified(self):
        graded, stats = grade_statements([
            statement("record", ["E0001", "E0025"], "멈추지 않게"),  # E0025: e.g. a later fix that was never sent
            statement("interpretation", ["E9999"]),
            statement("needs_verification", ["whatif"]),
        ], ITEMS)
        self.assertEqual([g["grade"] for g in graded], ["citation_unverified"] * 3)
        self.assertEqual([g["invalid_ids"] for g in graded], [["E0025"], ["E9999"], ["whatif"]])
        self.assertEqual(graded[0]["evidence_ids"], ["E0001", "E0025"])
        self.assertEqual(stats["citation_unverified"], 3)

    def test_shape_labels_and_stats(self):
        graded, stats = grade_statements([
            statement("record", ["E0001"], "timeout을 넣어 줘", text="요청에 기록됨"),
            statement("interpretation", ["E0002"]),
            statement("needs_verification", []),
            statement("record", ["E0002"], "다른 인용"),
        ], ITEMS)
        self.assertEqual(set(graded[0]), GRADED_KEYS)
        self.assertEqual(graded[0], {
            "text": "요청에 기록됨", "kind": "record", "evidence_ids": ["E0001"], "quote": "timeout을 넣어 줘",
            "grade": "recorded", "grade_label": "확인된 기록", "invalid_ids": [],
        })
        self.assertEqual([g["grade_label"] for g in graded[1:]], ["모델 추정", "검증 필요", "인용 미확인"])
        self.assertEqual(stats, {"total": 4, "recorded": 1, "inferred": 1, "needs_verification": 1,
                                 "citation_unverified": 1})
        self.assertEqual(grade_statements([], ITEMS), ([], {
            "total": 0, "recorded": 0, "inferred": 0, "needs_verification": 0, "citation_unverified": 0,
        }))


class GradePointsTest(unittest.TestCase):
    def test_review_points(self):
        points = [
            {"type": "issue", "text": "a", "evidence_ids": ["E0002"], "quote": "requests.get(url,   timeout=5)"},
            {"type": "judgment", "text": "b", "evidence_ids": ["E0002"], "quote": "지어낸 인용"},
            {"type": "alternative", "text": "c", "evidence_ids": ["E0001"], "quote": ""},
            {"type": "missing_evidence", "text": "d", "evidence_ids": [], "quote": ""},
            {"type": "counterexample", "text": "e", "evidence_ids": ["E0001", "E0042"], "quote": "멈추지 않게"},
        ]
        graded = grade_points(points, ITEMS)
        self.assertEqual([g["grade"] for g in graded], [
            "recorded", "citation_unverified", "inferred", "needs_verification", "citation_unverified",
        ])
        self.assertEqual([g["type_label"] for g in graded], ["문제", "판단", "대안", "근거 누락", "반례"])
        self.assertEqual(graded[4]["invalid_ids"], ["E0042"])
        self.assertEqual(set(graded[0]), {"type", "type_label", "text", "evidence_ids", "quote", "grade", "grade_label",
                                          "invalid_ids"})


if __name__ == "__main__":
    unittest.main()
