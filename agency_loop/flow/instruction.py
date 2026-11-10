"""Rule check of the user's AI instruction against the session's recorded test runs (ARCHITECTURE §5.7).

Shown next to the model's instruction_review. A recorded run is not a pass: statuses come from the rule engine.
"""

from __future__ import annotations

import re
from typing import Any

from agency_loop import labels
from agency_loop.decisions.testruns import test_status
from agency_loop.model import TestRun

# ASCII word rules, so a Korean particle right after a name ("test_api.py를") still ends the match.
_FILE = re.compile(r"\b(?:test_\w+\.py|\w+\.test\.[jt]sx?|\w+_test\.go)\b", re.ASCII)
_RUNNER = re.compile(r"\b(?:pytest|unittest|jest|npm (?:run )?test)\b", re.ASCII | re.IGNORECASE)
NOTE = ("세션 기록만으로 확인한 결과입니다. 명령에 이름이 드러난 실행만 연결하며, 분석 범위 밖의 실행은 "
        "다른 코드 버전의 결과로 표시합니다.")
NO_MENTIONS = "지시에서 테스트 파일이나 테스트 실행기 이름을 찾지 못했습니다."


def check_instruction(text: str, test_runs: list[TestRun], window: tuple[int, int]) -> dict[str, Any]:
    """Mentions of test files and runners, each with whether a run was recorded and its TEST_STATUS.

    A file matches a run whose command names it; a runner matches by command text or by the detected runner.
    The status is that of the last matching run inside the analysis window, `other_version` when every matching
    run is outside it, `none` without a match.
    """
    start, end = window
    mentions = [(name, "file") for name in dict.fromkeys(_FILE.findall(text))]
    mentions += [(name, "runner") for name in dict.fromkeys(m.lower() for m in _RUNNER.findall(text))]
    rows = []
    for name, kind in mentions:
        if kind == "file":
            runs = [run for run in test_runs if name in run.command]
        else:
            runs = [run for run in test_runs if name in run.command.lower() or name == run.runner]
        inside = [run for run in runs if start <= run.use_idx <= end]
        status = test_status(inside[-1]) if inside else "other_version" if runs else "none"
        rows.append({"text": name, "kind": kind, "found": bool(runs), "status": status,
                     "status_label": labels.TEST_STATUS[status]})
    return {"mentions": rows, "note": NOTE if rows else NO_MENTIONS}
