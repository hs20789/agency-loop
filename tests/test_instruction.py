"""agency_loop.flow.instruction: test files and runners named in the user's AI instruction (ARCHITECTURE §5.7)."""

import unittest

from agency_loop.flow.instruction import NO_MENTIONS, check_instruction
from agency_loop.model import TestRun

WINDOW = (10, 20)


def run(use_idx, command, runner="pytest", result="pass"):
    return TestRun(
        use_idx=use_idx, result_idx=use_idx + 1, command=command, runner=runner,
        exit_code={"pass": 0, "fail": 1}.get(result), exit_source="explicit", result=result, summary_line="요약",
        complete=True, interrupted=False,
    )


def rows(text, runs, window=WINDOW):
    return [(m["text"], m["kind"], m["found"], m["status"]) for m in check_instruction(text, runs, window)["mentions"]]


class CheckInstructionTest(unittest.TestCase):
    def test_file_and_runner_mentions_with_recorded_status(self):
        runs = [run(15, "pytest tests/test_weather.py -q", result="fail")]
        result = check_instruction("test_weather.py를 pytest로 실행해서 통과하는지 확인해 줘", runs, WINDOW)
        self.assertEqual(result["mentions"], [
            {"text": "test_weather.py", "kind": "file", "found": True, "status": "recorded_fail",
             "status_label": "기록된 실패"},
            {"text": "pytest", "kind": "runner", "found": True, "status": "recorded_fail", "status_label": "기록된 실패"},
        ])
        self.assertTrue(result["note"])

    def test_status_follows_the_analysis_window(self):
        before, inside, after = run(5, "pytest -q", result="fail"), run(18, "pytest -q"), run(30, "pytest -q", result="fail")
        self.assertEqual(rows("pytest", [before, inside, after]), [("pytest", "runner", True, "recorded_pass")])
        self.assertEqual(rows("pytest", [before, after]), [("pytest", "runner", True, "other_version")])
        unknown = run(12, "python -m pytest", result=None)
        self.assertEqual(rows("pytest", [unknown]), [("pytest", "runner", True, "ran_unknown")])  # ran, not passed
        self.assertEqual(rows("pytest", []), [("pytest", "runner", False, "none")])
        self.assertEqual(rows("test_api.py", [inside]), [("test_api.py", "file", False, "none")])  # not named

    def test_runners_match_by_command_or_detected_runner(self):
        npm = run(12, "npm test", runner="jest", result="fail")
        unittest_run = run(14, "python -m unittest discover -s tests")
        go = run(16, "go test ./...", runner=None, result=None)
        text = "Jest로 app.test.js를, npm test와 unittest, api_test.go, Button.test.tsx도 확인"
        self.assertEqual(rows(text, [npm, unittest_run, go]), [
            ("app.test.js", "file", False, "none"),
            ("api_test.go", "file", False, "none"),
            ("Button.test.tsx", "file", False, "none"),
            ("jest", "runner", True, "recorded_fail"),
            ("npm test", "runner", True, "recorded_fail"),
            ("unittest", "runner", True, "recorded_pass"),
        ])

    def test_mention_boundaries_and_duplicates(self):
        text = "mytest_api.py, test_api.pyc, test_x.py와 test_x.py, PYTEST, pytest-cov, mypytest"
        self.assertEqual(rows(text, []), [
            ("test_x.py", "file", False, "none"),
            ("pytest", "runner", False, "none"),
        ])

    def test_no_mentions(self):
        result = check_instruction("예외 처리를 꼼꼼히 해 줘", [run(15, "pytest")], WINDOW)
        self.assertEqual(result, {"mentions": [], "note": NO_MENTIONS})


if __name__ == "__main__":
    unittest.main()
