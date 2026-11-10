"""Retrospective prediction: eligibility, analysis window, pre-context, reveal and label (docs/ARCHITECTURE.md §4.4).

The target run and everything after its command stay on the server until the prediction is stored (§4.5).
"""

from __future__ import annotations

from typing import Any

from agency_loop import labels
from agency_loop.decisions.evidence import evidence_id
from agency_loop.decisions.testruns import test_status
from agency_loop.ingest.scope import EDIT_TOOLS
from agency_loop.model import Candidate, Event, Evidence, RetroInfo, RetroReason, TestRun, Workspace

OUTPUT_EXCERPT_CHARS = 3000


def retro_info(decision_idx: int, events: list[Event], test_runs: list[TestRun]) -> RetroInfo:
    """Eligibility of the first test-like run after the decision; the target is kept even when not eligible."""
    target = next((run for run in test_runs if run.use_idx > decision_idx), None)
    if target is None:
        return RetroInfo(eligible=False, reason="no_test")
    reason: RetroReason | None = None
    if any(e.kind == "tool_use" and e.tool in EDIT_TOOLS for e in events[decision_idx + 1 : target.use_idx]):
        reason = "code_changed"
    elif target.runner is None:
        reason = "unsupported_runner"
    elif target.result_idx is None:
        reason = "no_exit_code"
    elif target.interrupted:
        reason = "interrupted"
    elif not target.complete:
        reason = "truncated"
    elif target.exit_code is None:
        reason = "no_exit_code"
    elif target.result is None:
        reason = "inconsistent"
    return RetroInfo(eligible=reason is None, reason=reason, target=target)


def analysis_window(candidate: Candidate, events: list[Event]) -> tuple[int, int]:
    """(request_idx, end): end is the target result when one exists (eligible or not), else the unit's last event."""
    target = candidate.retro.target
    if target is not None and target.result_idx is not None:
        return candidate.request_idx, target.result_idx
    return candidate.request_idx, max(e.idx for e in events if e.turn == candidate.turn)


def precontext(ws: Workspace, candidate: Candidate) -> dict[str, Any]:
    """Records shown before the prediction: requests, the candidate's code edits and the target command."""
    target = candidate.retro.target
    last = next(e.idx for e in reversed(ws.session.events[: target.use_idx]) if e.kind == "user_text")
    return {
        "request_id": evidence_id(candidate.request_idx),
        "last_request_id": evidence_id(last) if last != candidate.request_idx else None,
        "edit_ids": [evidence_id(idx) for idx in candidate.edit_idxs],
        "command": {"evidence_id": evidence_id(target.use_idx), "command": target.command, "runner": target.runner},
    }


def reveal(target: TestRun, evidence: dict[str, Evidence]) -> dict[str, Any]:
    """The recorded result of an eligible target; served only in the prediction response."""
    result_id = evidence_id(target.result_idx)
    return {
        "command": target.command,
        "exit_code": target.exit_code,
        "exit_source": target.exit_source,
        "result": target.result,
        "result_label": labels.TEST_STATUS[test_status(target)],
        "summary_line": target.summary_line,
        "output_excerpt": evidence[result_id].excerpt[-OUTPUT_EXCERPT_CHARS:],
        "evidence_id": result_id,
    }


def label(choice: str, result: str) -> str:
    """labels.PREDICTION_LABEL key; "unknown" is withheld judgement, not a miss."""
    if choice == "unknown":
        return "withheld"
    return "match" if choice == result else "mismatch"


def checkpoints(target: TestRun) -> list[str]:
    if target.exit_source == "explicit":
        exit_line = f"종료 코드: {target.exit_code} (명시)"
    else:
        exit_line = "종료 코드: 0 (is_error=false로 추정)"
    return [exit_line, f"요약 줄: {target.summary_line}"]
