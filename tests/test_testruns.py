"""agency_loop.decisions.testruns: test detection, runner summaries, exit status, verdicts (ARCHITECTURE §4.2)."""

import unittest

from agency_loop.config import Settings
from agency_loop.decisions.candidates import file_kind
from agency_loop.decisions.evidence import edit_file
from agency_loop.decisions.retro import retro_info
from agency_loop.decisions.testruns import find_test_runs, test_status
from agency_loop.ingest.parser import parse_session
from tests.helpers import load_bytes
from tests.test_parser import ai, human, jsonl, result, use

# fixtures/README.md "예상 결과" for every request unit with a code edit: the retro reason, or the recorded result
# when the unit is eligible for the prediction.
README_OUTCOMES = {
    "demo_weather.jsonl": {1: "pass", 2: "fail", 3: "pass", 4: "no_test"},
    "pair_fail.jsonl": {1: "pass", 2: "fail"},
    "pair_pass.jsonl": {1: "pass", 2: "pass"},
    "no_tests.jsonl": {1: "no_test"},
    "truncated_output.jsonl": {1: "truncated"},
    "no_exit_code.jsonl": {1: "no_exit_code"},
    "code_changed.jsonl": {1: "code_changed"},
    "unsupported_runner.jsonl": {1: "unsupported_runner"},
    "secrets.jsonl": {1: "no_test"},
    "many_requests.jsonl": {**dict.fromkeys(range(1, 6), "code_changed"), 6: "pass"},
    "runners.jsonl": {1: "pass", 2: "fail", 3: "fail"},
}


def runs_of(name):
    return find_test_runs(parse_session(load_bytes(name)).events)


def run_of(command, output, *, is_error=False, interrupted=None):
    session = parse_session(jsonl(
        human("테스트 돌려 줘"), ai(use("t1", "Bash", {"command": command})),
        result("t1", output, is_error=is_error, interrupted=interrupted),
    ))
    [run] = find_test_runs(session.events)
    return run


def verdict(run):
    return run.runner, run.result, run.exit_code, run.exit_source, run.summary_line, run.complete


class FixtureRunsTest(unittest.TestCase):
    def test_runners_fixture(self):
        runs = runs_of("runners.jsonl")
        self.assertEqual([(r.use_idx, r.result_idx, r.command) for r in runs], [
            (5, 6, "python -m unittest"), (13, 14, "python -m unittest"), (21, 22, "npm test"),
        ])
        self.assertEqual([verdict(r) for r in runs], [
            ("unittest", "pass", 0, "is_error_false", "OK", True),
            ("unittest", "fail", 1, "explicit", "FAILED (failures=1)", True),
            ("jest", "fail", 1, "explicit", "Tests:       1 failed, 5 passed, 6 total", True),
        ])

    def test_demo_weather_runs(self):
        runs = runs_of("demo_weather.jsonl")
        self.assertEqual([(r.use_idx, r.result_idx) for r in runs], [(7, 8), (18, 19), (28, 29)])
        self.assertEqual([verdict(r) for r in runs], [
            ("pytest", "pass", 0, "is_error_false", "3 passed in 0.31s", True),
            ("pytest", "fail", 1, "explicit", "1 failed, 3 passed in 0.42s", True),
            ("pytest", "pass", 0, "is_error_false", "4 passed in 0.40s", True),
        ])

    def test_fixture_runs_that_cannot_be_judged(self):
        [truncated] = runs_of("truncated_output.jsonl")
        self.assertEqual(verdict(truncated), ("pytest", None, 1, "explicit", None, False))
        [no_exit] = runs_of("no_exit_code.jsonl")
        self.assertEqual(verdict(no_exit), ("pytest", None, None, None, "1 failed, 3 passed in 0.38s", True))
        [go] = runs_of("unsupported_runner.jsonl")
        self.assertEqual(verdict(go), (None, None, 0, "is_error_false", None, False))
        self.assertEqual(runs_of("no_tests.jsonl"), [])

    def test_readme_expected_outcomes(self):
        for name, expected in README_OUTCOMES.items():
            with self.subTest(name=name):
                session = parse_session(load_bytes(name))
                runs = find_test_runs(session.events)
                decisions = {}  # turn -> last code edit of the unit
                for event in session.events:
                    if event.turn >= 1 and file_kind(edit_file(event) or ""):
                        decisions[event.turn] = event.idx
                outcomes = {}
                for turn, idx in decisions.items():
                    info = retro_info(idx, session.events, runs)
                    outcomes[turn] = info.reason or info.target.result
                self.assertEqual(outcomes, expected)
        requests = parse_session(load_bytes("many_requests.jsonl")).events
        self.assertGreaterEqual(sum(e.kind == "user_text" for e in requests), Settings().rag_min_requests)


class SummaryAndExitTest(unittest.TestCase):
    def test_pytest_summary_forms(self):
        cases = [
            ("Exit code 1\n===== 1 failed, 2 passed, 1 warning in 0.12s =====", True,
             ("fail", 1, "===== 1 failed, 2 passed, 1 warning in 0.12s =====")),
            ("==== 3 passed in 75.20s (0:01:15) ====", False, ("pass", 0, "==== 3 passed in 75.20s (0:01:15) ====")),
            ("Exit code 1\n==== 1 error in 0.10s ====", True, ("fail", 1, "==== 1 error in 0.10s ====")),
            ("Error: Exit code 1\nFAILED t.py::x\n1 failed in 0.1s", True, ("fail", 1, "1 failed in 0.1s")),
            ("1 failed in 0.1s\nretry\n2 passed in 0.1s", False, ("pass", 0, "2 passed in 0.1s")),  # last summary wins
            ("==== 2 skipped in 0.01s ====", False, (None, 0, "==== 2 skipped in 0.01s ====")),
        ]
        for output, is_error, expected in cases:
            with self.subTest(output=output):
                run = run_of("python -m pytest -q", output, is_error=is_error)
                self.assertEqual((run.runner, run.complete), ("pytest", True))
                self.assertEqual((run.result, run.exit_code, run.summary_line), expected)

    def test_unittest_uses_the_last_ran_block(self):
        run = run_of("python3 -m unittest discover", "Exit code 1\nRan 1 test in 0.001s\n\nOK\nRan 2 tests in 0.002s\n\nFAILED (errors=1)", is_error=True)
        self.assertEqual(verdict(run), ("unittest", "fail", 1, "explicit", "FAILED (errors=1)", True))
        run = run_of("python -m unittest", "Ran 1 test in 0.001s\n\nOK\nRan 2 tests in 0.002s\nTraceback")
        self.assertEqual((run.summary_line, run.result, run.complete), (None, None, False))

    def test_jest_commands_need_a_tests_line(self):
        output = "Exit code 1\nTests:       2 failed, 1 passed, 3 total"
        for command in ("npm test", "npm run test -- --ci", "npx jest src", "jest --ci"):
            with self.subTest(command=command):
                self.assertEqual(run_of(command, output, is_error=True).runner, "jest")
        vitest = run_of("npm test", " Test Files  1 passed (1)\n      Tests  3 passed (3)")
        self.assertEqual((vitest.runner, vitest.result, vitest.summary_line), (None, None, None))

    def test_verdict_must_agree_with_the_exit_code(self):
        cases = [
            ("pytest", "1 failed in 0.1s", False),  # failure but exit 0
            ("pytest", "Exit code 1\n3 passed in 0.1s", True),  # pass but exit 1
            ("pytest", "Exit code 2\n==== 1 error in 0.10s ====", True),  # pytest test failures exit 1
            ("npm test", "Exit code 1\nTests:       3 passed, 3 total", True),
            ("python -m unittest", "Ran 1 test in 0.1s\n\nFAILED (failures=1)", False),
            ("pytest", "1 failed, 3 passed in 0.38s", True),  # is_error without "Exit code": unknown exit
        ]
        for command, output, is_error in cases:
            with self.subTest(output=output):
                run = run_of(command, output, is_error=is_error)
                self.assertIsNotNone(run.summary_line)
                self.assertIsNone(run.result)
        self.assertEqual(run_of("jest", "Exit code 3\nTests:       1 failed, 1 total", is_error=True).result, "fail")

    def test_truncation_markers_make_the_run_incomplete(self):
        for marker in ("[... 출력 일부 생략 ...]", "... [1200 lines truncated] ...", "[1200 lines truncated]",
                       "Output truncated (30000 chars)", "<response clipped>", "OUTPUT TOO LARGE", "... [30 lines"):
            with self.subTest(marker=marker):
                run = run_of("pytest", f"start\n{marker}\n3 passed in 0.1s")
                self.assertEqual((run.summary_line, run.result, run.complete), ("3 passed in 0.1s", "pass", False))

    def test_the_bare_word_truncated_is_not_a_marker(self):
        run = run_of("pytest -v", "tests/test_parse.py::test_truncated_input PASSED\n1 passed in 0.1s")
        self.assertEqual((run.result, run.complete), ("pass", True))

    def test_hostile_long_line_is_not_a_summary(self):
        run = run_of("pytest", "= " + " in 1s (" * 20_000 + "\n1 passed in 0.1s")
        self.assertEqual((run.summary_line, run.result), ("1 passed in 0.1s", "pass"))


class DetectionTest(unittest.TestCase):
    def test_only_test_like_bash_commands_are_runs(self):
        commands = ["ls", "pip list", "npm install", "echo tests", "go build ./...", "pip install pytest",
                    "cat pytest.ini", "rm -rf .pytest_cache", "git commit -m 'fix pytest'", "python -m unittest.mock"]
        session = parse_session(jsonl(
            human("명령 실행"), ai(*[use(f"t{n}", "Bash", {"command": c}) for n, c in enumerate(commands)]),
            ai(use("e1", "Edit", {"file_path": "test_x.py", "old_string": "pytest", "new_string": "pytest -q"})),
        ))
        self.assertEqual([r.command for r in find_test_runs(session.events)], [])

    def test_a_segment_starting_with_a_test_command_is_a_run(self):
        # Segments split on && || ; | and newlines; assignments, cd segments and wrapper prefixes are skipped.
        cases = {
            "cd app && uv run pytest -q": "pytest", "VAR=1 python -m pytest": "pytest", "npx jest": "jest",
            "timeout 60 npm test": "jest", "pip install -q requests; .venv/bin/python -m unittest": "unittest",
            "pip install pytest && npm test": "jest",  # the runner comes from the test segment only
            "PYTHONPATH=. poetry run pytest | tail -5": "pytest",
        }
        output = "Exit code 1\nTests:       1 failed, 1 total"
        for command, runner in cases.items():
            with self.subTest(command=command):
                self.assertEqual(run_of(command, output, is_error=True).runner, runner)

    def test_unsupported_test_runners(self):
        for command in ("go test ./...", "cargo test", "npx vitest run", "vitest", "mvn -q test", "mvn clean test",
                        "./gradlew test", "rspec", "phpunit"):
            with self.subTest(command=command):
                run = run_of(command, "ok")
                self.assertEqual((run.runner, run.result, run.summary_line), (None, None, None))

    def test_results_are_joined_by_link_id(self):
        session = parse_session(jsonl(
            human("테스트"),
            ai(use("t1", "Bash", {"command": "pytest -q"}), use("t2", "Bash", {"command": "ls"}), use("t3", "Bash", {"command": "pytest"})),
            result("t2", "a.py"), result("t1", "Exit code 1\n1 failed in 0.1s", is_error=True, interrupted=True),
        ))
        first, second = find_test_runs(session.events)
        self.assertEqual((first.use_idx, first.result_idx, first.result, first.interrupted), (1, 5, "fail", True))
        self.assertEqual((second.use_idx, second.result_idx, second.exit_code, second.complete, second.interrupted),
                         (3, None, None, False, False))

    def test_test_status(self):
        demo = runs_of("demo_weather.jsonl")
        self.assertEqual(test_status(None), "none")
        self.assertEqual([test_status(r) for r in demo], ["recorded_pass", "recorded_fail", "recorded_pass"])
        self.assertEqual(test_status(runs_of("truncated_output.jsonl")[0]), "ran_unknown")


if __name__ == "__main__":
    unittest.main()
