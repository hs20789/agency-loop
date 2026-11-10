"""agency_loop.decisions.retro: eligibility, window, pre-context, reveal, label, checkpoints (ARCHITECTURE §4.4)."""

import json
import unittest

from agency_loop.config import Settings
from agency_loop.decisions.candidates import analyze_session
from agency_loop.decisions.retro import analysis_window, checkpoints, label, precontext, reveal
from agency_loop.ingest.parser import parse_session
from agency_loop.model import Workspace
from tests.helpers import PAIR_MARKERS, load_bytes
from tests.test_parser import ai, human, jsonl, result, use


def analyzed(session):
    evidence, test_runs, candidates = analyze_session(session, Settings())
    ws = Workspace(id="ws", created_at=0.0, last_used=0.0, session=session, evidence=evidence, test_runs=test_runs,
                   candidates=candidates)
    return ws, {c.turn: c for c in candidates}


def fixture(name):
    return analyzed(parse_session(load_bytes(name)))


def one_run(output, *, is_error=False, interrupted=None, with_result=True):
    """A unit with one code edit followed by `pytest -q` and (optionally) its result."""
    records = [human("timeout 처리 넣어 줘"), ai(use("t1", "Edit", {"file_path": "w.py", "old_string": "a", "new_string": "b"})),
               result("t1", "updated"), ai(use("t2", "Bash", {"command": "pytest -q"}))]
    if with_result:
        records.append(result("t2", output, is_error=is_error, interrupted=interrupted))
    return analyzed(parse_session(jsonl(*records)))


class EligibilityTest(unittest.TestCase):
    def test_demo_weather_windows(self):
        ws, by_turn = fixture("demo_weather.jsonl")
        events = ws.session.events
        turn2 = by_turn[2]
        self.assertEqual((turn2.retro.eligible, turn2.retro.reason), (True, None))
        self.assertEqual((turn2.retro.target.use_idx, turn2.retro.target.result_idx, turn2.retro.target.result), (18, 19, "fail"))
        self.assertEqual(analysis_window(turn2, events), (10, 19))
        turn3_fix = next(e.idx for e in events if e.turn == 3 and e.kind == "tool_use" and e.tool == "Edit")
        self.assertGreater(turn3_fix, analysis_window(turn2, events)[1])
        self.assertTrue(turn2.later_fix)
        self.assertEqual((by_turn[1].retro.target.result, analysis_window(by_turn[1], events)), ("pass", (0, 8)))
        self.assertEqual((by_turn[4].retro.reason, analysis_window(by_turn[4], events)), ("no_test", (31, 41)))

    def test_fixture_reasons(self):
        expected = {
            "truncated_output.jsonl": "truncated", "no_exit_code.jsonl": "no_exit_code", "code_changed.jsonl": "code_changed",
            "unsupported_runner.jsonl": "unsupported_runner", "no_tests.jsonl": "no_test",
        }
        for name, reason in expected.items():
            with self.subTest(name=name):
                _, by_turn = fixture(name)
                self.assertEqual((by_turn[1].retro.eligible, by_turn[1].retro.reason), (False, reason))
        _, no_tests = fixture("no_tests.jsonl")
        self.assertEqual((no_tests[1].has_test_record, no_tests[1].retro.target), (False, None))
        ws, changed = fixture("code_changed.jsonl")
        self.assertEqual(changed[1].retro.target.use_idx, 7)  # the target is kept for the window
        self.assertEqual(analysis_window(changed[1], ws.session.events), (0, 8))

    def test_runners_fixture_is_eligible_with_rule_engine_results(self):
        _, by_turn = fixture("runners.jsonl")
        self.assertTrue(all(c.retro.eligible for c in by_turn.values()))
        self.assertEqual({turn: c.retro.target.result for turn, c in by_turn.items()}, {1: "pass", 2: "fail", 3: "fail"})

    def test_reasons_from_the_target_run(self):
        cases = [
            ({"output": "Exit code 1\n1 failed in 0.1s", "is_error": True, "interrupted": True}, "interrupted"),
            ({"output": "Exit code 1\n3 passed in 0.1s", "is_error": True}, "inconsistent"),
            ({"output": "", "with_result": False}, "no_exit_code"),
        ]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                ws, by_turn = one_run(**kwargs)
                self.assertEqual((by_turn[1].retro.eligible, by_turn[1].retro.reason), (False, reason))
        ws, by_turn = one_run("", with_result=False)
        self.assertEqual(analysis_window(by_turn[1], ws.session.events), (0, 3))  # no result: the unit's end


class PrecontextAndRevealTest(unittest.TestCase):
    def test_demo_precontext(self):
        ws, by_turn = fixture("demo_weather.jsonl")
        self.assertEqual(precontext(ws, by_turn[2]), {
            "request_id": "E0010",
            "last_request_id": None,
            "edit_ids": ["E0012", "E0015"],
            "command": {"evidence_id": "E0018", "command": "pytest -q", "runner": "pytest"},
        })

    def test_pair_precontext_is_identical_and_blind(self):
        texts = []
        for name in ("pair_fail.jsonl", "pair_pass.jsonl"):
            ws, by_turn = fixture(name)
            texts.append(json.dumps(precontext(ws, by_turn[2]), ensure_ascii=False))
            for marker in PAIR_MARKERS:
                self.assertNotIn(marker, texts[-1])
        self.assertEqual(texts[0], texts[1])

    def test_demo_reveal(self):
        ws, by_turn = fixture("demo_weather.jsonl")
        shown = reveal(by_turn[2].retro.target, ws.evidence)
        self.assertEqual({key: value for key, value in shown.items() if key != "output_excerpt"}, {
            "command": "pytest -q", "exit_code": 1, "exit_source": "explicit", "result": "fail", "result_label": "기록된 실패",
            "summary_line": "1 failed, 3 passed in 0.42s", "evidence_id": "E0019",
        })
        self.assertEqual(shown["output_excerpt"], ws.session.events[19].text)
        self.assertIn("<HOME>/weather-app/weather.py", shown["output_excerpt"])
        self.assertEqual(reveal(by_turn[1].retro.target, ws.evidence)["result_label"], "기록된 통과")

    def test_reveal_keeps_the_last_3000_chars(self):
        output = "Exit code 1\n" + "x" * 5000 + "\n1 failed in 0.1s"
        ws, by_turn = one_run(output, is_error=True)
        self.assertTrue(by_turn[1].retro.eligible)
        self.assertEqual(reveal(by_turn[1].retro.target, ws.evidence)["output_excerpt"], output[-3000:])

    def test_label(self):
        self.assertEqual([label("unknown", "fail"), label("fail", "fail"), label("pass", "pass"), label("pass", "fail")],
                         ["withheld", "match", "match", "mismatch"])

    def test_checkpoints(self):
        _, by_turn = fixture("demo_weather.jsonl")
        self.assertEqual(checkpoints(by_turn[2].retro.target), ["종료 코드: 1 (명시)", "요약 줄: 1 failed, 3 passed in 0.42s"])
        self.assertEqual(checkpoints(by_turn[1].retro.target), ["종료 코드: 0 (is_error=false로 추정)", "요약 줄: 3 passed in 0.31s"])


if __name__ == "__main__":
    unittest.main()
