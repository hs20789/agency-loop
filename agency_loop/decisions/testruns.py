"""Test runs in Bash tool uses and the rule-engine verdict (docs/ARCHITECTURE.md §4.2).

Only this module turns a recorded run into pass/fail: "test ran" is not "test passed".
"""

from __future__ import annotations

import re

from agency_loop.ingest.parser import TRUNCATION_MARK
from agency_loop.model import Event, TestRun

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
# Leading VAR=value assignments and wrapper prefixes in front of the real command.
_PREFIX = re.compile(r"\s*(?:(?:[A-Za-z_]\w*=\S*|(?:uv|poetry|pipenv)\s+run|npx|timeout\s+\S+)\s+)*")
# A test command at the start of a segment, also when called by path (./gradlew, .venv/bin/python).
_TEST_START = re.compile(
    r"(?:[\w.~/-]*/)?(?:(?P<unittest>python[0-9.]*\s+-m\s+unittest)|(?P<pytest>pytest|python[0-9.]*\s+-m\s+pytest)"
    r"|(?P<jest>npm\s+(?:run\s+)?test|jest)|vitest|go\s+test|cargo\s+test|mvn\b.*\stest|gradlew?\s+test|phpunit|rspec)"
    r"(?![\w.-])"
)

_EXIT = re.compile(r"(?:Error: )?Exit code (\d+)")
# The bare word "truncated" is no marker: test names such as test_truncated_input contain it.
_TRUNCATED = re.compile(
    re.escape(TRUNCATION_MARK.strip()) + r"|lines truncated|output truncated|<response clipped>|Output too large"
    r"|\.\.\. \[\d+ lines",
    re.IGNORECASE,
)
_PYTEST_LONG = re.compile(r"^=+ (.+) in [0-9.]+s( \(.*\))? =+$")
_PYTEST_SHORT = re.compile(r"^(\d+ (failed|passed)[^=]*) in [0-9.]+s$")
_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$")
_SUMMARY = {"pytest": (_PYTEST_LONG, _PYTEST_SHORT), "jest": (_JEST_TESTS,)}
_COUNT = re.compile(r"(\d+) (failed|passed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in")
_UNITTEST_END = re.compile(r"^(OK|FAILED \()")
# Real summary lines are short; skipping long lines bounds regex backtracking on hostile uploads.
_MAX_SUMMARY_LINE = 500


def find_test_runs(events: list[Event]) -> list[TestRun]:
    """Every test-like Bash tool use in file order, joined to the first later tool_result with its link id."""
    waiting: dict[str, Event] = {}  # link id -> test-like use without a result yet
    found: dict[int, tuple[Event, Event | None]] = {}  # use idx -> (use, result)
    for event in events:
        if event.kind == "tool_use" and event.tool == "Bash" and _test_segment(_command(event)):
            found[event.idx] = (event, None)
            if event.tool_use_id:
                waiting[event.tool_use_id] = event
        elif event.kind == "tool_result" and event.tool_use_id in waiting:
            use = waiting.pop(event.tool_use_id)
            found[use.idx] = (use, event)
    return [_run(use, output) for use, output in found.values()]


def test_status(run: TestRun | None) -> str:
    """labels.TEST_STATUS key of a run (`other_version` is decided by the instruction check)."""
    if run is None:
        return "none"
    if run.result is None:
        return "ran_unknown"
    return f"recorded_{run.result}"


def _command(event: Event) -> str:
    return (event.tool_input or {}).get("command", "")


def _test_segment(command: str) -> re.Match[str] | None:
    """The first command segment that starts with a test command once assignments and wrappers are stripped."""
    for segment in _SEGMENT_SPLIT.split(command):
        if match := _TEST_START.match(segment, _PREFIX.match(segment).end()):
            return match
    return None


def _run(use: Event, output: Event | None) -> TestRun:
    command = _command(use)
    text = output.text if output else ""
    runner = _runner(_test_segment(command), text)
    exit_code, exit_source = _exit_status(output)
    summary_line, result = _summary(runner, text)
    # The verdict must agree with the exit code; pytest exits 1 on test failures.
    if result == "pass" and exit_code != 0:
        result = None
    if result == "fail" and (exit_code in (None, 0) or (runner == "pytest" and exit_code != 1)):
        result = None
    return TestRun(
        use_idx=use.idx,
        result_idx=output.idx if output else None,
        command=command,
        runner=runner,
        exit_code=exit_code,
        exit_source=exit_source,
        result=result,
        summary_line=summary_line,
        complete=summary_line is not None and not _TRUNCATED.search(text),
        interrupted=bool(output and output.interrupted),
    )


def _runner(test: re.Match[str], output: str) -> str | None:
    """Runner of the test segment; npm test and jest count as Jest only with a Jest `Tests:` line."""
    if test["unittest"]:
        return "unittest"
    if test["pytest"]:
        return "pytest"
    if test["jest"] and any(_JEST_TESTS.match(line) for line in output.splitlines()):
        return "jest"
    return None


def _exit_status(output: Event | None) -> tuple[int | None, str | None]:
    if output is None:
        return None, None
    match = _EXIT.match(output.text)
    if match:
        return int(match.group(1)), "explicit"
    if output.is_error is False:
        return 0, "is_error_false"
    return None, None


def _summary(runner: str | None, text: str) -> tuple[str | None, str | None]:
    """(summary line, verdict from that line); the last summary in the output wins."""
    lines = [line for line in text.splitlines() if len(line) <= _MAX_SUMMARY_LINE]
    if runner == "unittest":
        summary = None
        ran = False
        for line in lines:
            if _UNITTEST_RAN.match(line):
                ran, summary = True, None
            elif ran and summary is None and _UNITTEST_END.match(line):
                summary = line
        return summary, None if summary is None else "pass" if summary.startswith("OK") else "fail"
    last = None
    for line in lines:
        for pattern in _SUMMARY.get(runner or "", ()):
            if match := pattern.match(line):
                last = line, match.group(1)
    if last is None:
        return None, None
    counts = {"failed": 0, "passed": 0}
    for number, word in _COUNT.findall(last[1]):
        key = "failed" if word.startswith("error") else word  # pytest errors fail the run
        counts[key] = counts.get(key, 0) + int(number)
    if counts["failed"]:
        return last[0], "fail"
    return last[0], "pass" if counts["passed"] else None
