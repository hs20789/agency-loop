"""agency_loop.decisions.evidence: ids, kinds, diffs, excerpt caps and the blinding gate (ARCHITECTURE §4.1, §4.5)."""

import json
import unittest

from agency_loop.config import Settings
from agency_loop.decisions.candidates import analyze_session
from agency_loop.decisions.evidence import build_evidence, diff_lines, visible_evidence_ids
from agency_loop.ingest.parser import parse_session
from agency_loop.model import Workspace
from tests.helpers import PAIR_MARKERS, load_bytes
from tests.test_parser import ai, human, jsonl, result, text, use

PREDICTION = {"choice": "fail", "reason": "", "label": "match", "submitted_at": 0.0}


def workspace(session, **fields):
    evidence, test_runs, candidates = analyze_session(session, Settings())
    return Workspace(
        id="ws", created_at=0.0, last_used=0.0, session=session, evidence=evidence, test_runs=test_runs,
        candidates=candidates, **fields,
    )


def ids(start, end):
    return [f"E{idx:04d}" for idx in range(start, end + 1)]


class BuildEvidenceTest(unittest.TestCase):
    def test_one_item_per_event_with_source_position_and_kind(self):
        session = parse_session(load_bytes("demo_weather.jsonl"))
        evidence = build_evidence(session, 4000)
        self.assertEqual(list(evidence), ids(0, len(session.events) - 1))
        for event, item in zip(session.events, evidence.values(), strict=True):
            self.assertEqual((item.id, item.idx, item.line, item.turn), (f"E{event.idx:04d}", event.idx, event.line, event.turn))
        kinds = {idx: evidence[f"E{idx:04d}"].kind for idx in (0, 1, 2, 3, 7, 8, 22, 23, 33)}
        self.assertEqual(kinds, {
            0: "user_request", 1: "assistant_text", 2: "code_edit", 3: "tool_other", 7: "command", 8: "command_output",
            22: "tool_other", 23: "tool_other", 33: "tool_other",
        })
        self.assertEqual((evidence["E0007"].excerpt, evidence["E0022"].excerpt, evidence["E0033"].excerpt),
                         ("pytest -q", "Read weather.py", "TodoWrite"))
        self.assertEqual(evidence["E0010"].excerpt, session.events[10].text)
        self.assertEqual(
            [item.file for item in evidence.values() if item.kind == "code_edit"],
            ["weather.py", "tests/test_weather.py", "weather.py", "tests/test_weather.py", "weather.py", "config.toml",
             "requirements.txt", "weather.py"],
        )
        self.assertTrue(all(item.file is None for item in evidence.values() if item.kind != "code_edit"))

    def test_diff_text_and_viewer_rows_per_edit_tool(self):
        session = parse_session(jsonl(
            human("코드 고쳐 줘"),
            ai(
                use("t1", "Edit", {"file_path": "a.py", "old_string": "x = 1\ny = 2", "new_string": "x = 1\ny = 3"}),
                use("t2", "MultiEdit", {"file_path": "b.py", "edits": [
                    {"old_string": "a", "new_string": "b"}, {"old_string": "", "new_string": "c\n- d"},
                ]}),
                use("t3", "Write", {"file_path": "c.py", "content": "print(1)\n\nprint(2)\n"}),
                use("t4", "NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "import os"}),
            ),
        ))
        evidence = build_evidence(session, 4000)
        self.assertEqual([evidence[i].excerpt for i in ids(1, 4)], [
            "--- a.py\n- x = 1\n- y = 2\n+ x = 1\n+ y = 3",
            "--- b.py\n- a\n+ b\n@@\n+ c\n+ - d",
            "--- c.py\n+ print(1)\n+ \n+ print(2)",
            "--- n.ipynb\n+ import os",
        ])
        self.assertEqual([evidence[i].file for i in ids(1, 4)], ["a.py", "b.py", "c.py", "n.ipynb"])
        self.assertEqual(diff_lines(evidence["E0002"]), [
            {"op": "@", "text": "--- b.py"}, {"op": "-", "text": "a"}, {"op": "+", "text": "b"}, {"op": "@", "text": "@@"},
            {"op": "+", "text": "c"}, {"op": "+", "text": "- d"},
        ])
        self.assertEqual(diff_lines(evidence["E0003"])[2], {"op": "+", "text": ""})
        self.assertEqual(diff_lines(evidence["E0000"]), [])

    def test_excerpts_are_capped_and_outputs_keep_the_tail(self):
        request = "요청" + "가" * 50
        command = "pytest " + "-v " * 20
        output = "시작\n" + "x" * 50 + "\n3 passed in 0.1s"
        session = parse_session(jsonl(human(request), ai(use("t1", "Bash", {"command": command})), result("t1", output)))
        capped = build_evidence(session, 20)
        self.assertEqual([item.excerpt for item in capped.values()], [request[:19] + "…", command[:19] + "…", "…" + output[-19:]])
        self.assertTrue(capped["E0002"].excerpt.endswith("3 passed in 0.1s"))
        self.assertEqual([item.excerpt for item in build_evidence(session, 4000).values()], [request, command, output])


class VisibleEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.ws = workspace(parse_session(load_bytes("demo_weather.jsonl")))
        self.later_fixes = ["E0025", "E0039"]  # turn 3 and turn 4 edits of weather.py

    def test_nothing_is_visible_before_selection(self):
        self.assertEqual(visible_evidence_ids(self.ws), [])
        self.ws.selected = "c9"  # unknown id fails closed
        self.assertEqual(visible_evidence_ids(self.ws), [])

    def test_eligible_candidate_shows_only_the_precontext_until_the_prediction(self):
        self.ws.selected = "c1"
        candidate = self.ws.candidates[0]
        self.assertEqual((candidate.turn, candidate.retro.eligible), (2, True))
        self.ws.results["retrieval"] = {"retrieved": ["E0021"]}  # retrieval cannot widen the view before the prediction
        visible = visible_evidence_ids(self.ws)
        self.assertEqual(visible, ["E0010", "E0012", "E0015", "E0018"])
        self.assertTrue(all(self.ws.evidence[i].idx <= candidate.retro.target.use_idx for i in visible))

    def test_prediction_opens_the_analysis_window_but_never_later_fixes(self):
        self.ws.selected, self.ws.prediction = "c1", PREDICTION
        self.assertEqual(visible_evidence_ids(self.ws), ids(10, 19))
        self.ws.results["retrieval"] = {"retrieved": ["E0021", "E0025", "E0039", "E0029", "E9999"]}
        visible = visible_evidence_ids(self.ws)
        self.assertEqual(visible, [*ids(10, 19), "E0021"])  # only human requests are added
        for fix in self.later_fixes:
            self.assertNotIn(fix, visible)

    def test_not_eligible_candidate_shows_its_window_at_once(self):
        self.ws.selected = "c2"
        self.assertEqual((self.ws.candidates[1].turn, self.ws.candidates[1].retro.reason), (4, "no_test"))
        self.assertEqual(visible_evidence_ids(self.ws), ids(31, 41))

    def test_last_request_before_a_later_run_is_part_of_the_precontext(self):
        ws = workspace(parse_session(jsonl(
            human("timeout 처리 넣어 줘"),
            ai(text("넣겠습니다. 보통 1초 안에 응답한다고 가정합니다."),
               use("t1", "Edit", {"file_path": "w.py", "old_string": "a", "new_string": "b"})),
            result("t1", "updated"),
            human("이제 테스트 돌려 줘"),
            ai(use("t2", "Bash", {"command": "pytest -q"})),
            result("t2", "1 passed in 0.01s"),
            ai(text("통과했습니다.")),
        )), selected="c1")
        self.assertEqual(visible_evidence_ids(ws), ["E0000", "E0002", "E0004", "E0005"])
        ws.prediction = PREDICTION
        self.assertEqual(visible_evidence_ids(ws), ids(0, 6))

    def test_pair_fixtures_show_the_same_records_before_the_prediction(self):
        shown = []
        for name in ("pair_fail.jsonl", "pair_pass.jsonl"):
            ws = workspace(parse_session(load_bytes(name)), selected="c1")
            visible = visible_evidence_ids(ws)
            blob = json.dumps([ws.evidence[i].excerpt for i in visible], ensure_ascii=False)
            for marker in PAIR_MARKERS:
                self.assertNotIn(marker, blob)
            shown.append((visible, blob))
            ws.prediction = PREDICTION
            after = json.dumps([ws.evidence[i].excerpt for i in visible_evidence_ids(ws)], ensure_ascii=False)
            self.assertTrue(any(marker in after for marker in PAIR_MARKERS))
        self.assertEqual(shown[0], shown[1])


if __name__ == "__main__":
    unittest.main()
