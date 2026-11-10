"""ScopeView: what the upload can and cannot support (docs/ARCHITECTURE.md §3.4, §7.3).

Shown before any prediction, so nothing here may depend on tool outputs or test results.
"""

from __future__ import annotations

from typing import Any

from agency_loop import labels
from agency_loop.model import Session, TestRun

EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")

# ParseReport.dropped / skipped_record_types codes -> Korean category; unknown codes are "기타 기록".
_DROPPED = {
    "thinking": "생각 과정",
    "image": "이미지",
    "meta_fields": "세션·계정 메타데이터",
    "summary": "세션 요약 줄",
    "sidechain": "보조 에이전트 기록",
    "meta": "명령·메타 기록",
    "command_wrapper": "명령·메타 기록",
    "interrupt_marker": "명령·메타 기록",
    "system_reminder": "시스템 알림",
    "other_tool_input": "기타 도구 입력",
}
_OTHER = "기타 기록"
_DROPPED_ORDER = [*dict.fromkeys(_DROPPED.values()), _OTHER]


def build_scope(session: Session, test_runs: list[TestRun]) -> dict[str, Any]:
    # Imported here: decisions.evidence and decisions.retro import EDIT_TOOLS from this module.
    from agency_loop.decisions.candidates import file_kind  # the candidates' code/config file filter
    from agency_loop.decisions.evidence import edit_file

    report, in_text = session.report, session.redaction.in_text
    masked = {code: in_text[code] for code in labels.MASKED if in_text.get(code)}
    dropped = {_DROPPED.get(code, _OTHER) for code in [*report.dropped, *report.skipped_record_types]}
    bad_lines = len(report.bad_lines)
    notices = ["수업 기간에는 공개·연습용 세션만 올려 주세요"]
    if bad_lines:
        notices.append(f"깨진 줄 {bad_lines}개는 제외했습니다")
    notices.append("저장소 전체 코드는 없고 세션에 기록된 코드만 분석합니다")
    return {
        "format": "claude_code_jsonl",
        "human_requests": sum(event.kind == "user_text" for event in session.events),
        "has_code_edits": any(file_kind(edit_file(event) or "") for event in session.events),
        "has_test_records": bool(test_runs),
        "masked": masked,
        "masked_labels": {code: labels.MASKED[code] for code in masked},
        "dropped": [name for name in _DROPPED_ORDER if name in dropped],
        "bad_lines": bad_lines,
        "notices": notices,
    }
