"""Allowlisted JSON payloads (ARCHITECTURE §7.3). The only module that builds API payloads.

Blinding (§4.5): before selection only cards; selected and retro eligible without a stored prediction, only the
pre-context. Visibility of evidence ids is decided by `visible_evidence_ids`, never here. The stored results
(ws.results) are built here too, so a job's result and the workspace view share one shape.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.decisions.evidence import diff_lines, edit_file, visible_evidence_ids
from agency_loop.decisions.retro import analysis_window, checkpoints, precontext, reveal
from agency_loop.flow.bundle import Bundle
from agency_loop.ingest.scope import build_scope
from agency_loop.model import Candidate, Evidence, Job, Workspace
from agency_loop.providers.base import LLMResult

_RETRYABLE = frozenset({"rate_limited", "server_busy"})
RETRO_QUESTION = "이 결정 바로 뒤에 실행한 테스트는 기록상 어떤 결과였을까요?"
RESULT_KEYS = ("analysis", "report", "summary", "retrieval", "reviews", "comparison", "whatif")
PREDICTION_NOTE = (
    "일치 여부는 참고용 표시입니다. 결과를 맞혔다고 검증 능력이 높다는 뜻은 아니며, 점수나 능력 판정에 쓰지 않습니다. "
    "예측과 이유는 어떤 모델에도 보내지 않습니다."
)
SUMMARY_LABEL = "요약(Local LLM, 화면 표시용 — 근거나 다른 모델의 입력으로 쓰지 않음)"


def error_dict(code: str, message: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message or labels.ERROR[code], "retryable": code in _RETRYABLE}


def error(code: str, status: int, message: str | None = None) -> JSONResponse:
    """`message` (Korean, safe to show) replaces the code's default one."""
    return JSONResponse({"error": error_dict(code, message)}, status_code=status)


def health_view(settings: Settings) -> dict[str, Any]:
    s = settings
    modes = {"anthropic": s.anthropic_mode, "clova": s.clova_mode, "mistral": s.mistral_mode,
             "embed": s.embed_mode, "local_llm": s.local_llm_mode}
    return {"ok": True, "modes": modes}


def selected(ws: Workspace) -> Candidate | None:
    return next((c for c in ws.candidates if c.id == ws.selected), None)


def _blinded(ws: Workspace, candidate: Candidate) -> bool:
    return candidate.retro.eligible and ws.prediction is None


def card(ws: Workspace, c: Candidate) -> dict[str, Any]:
    """No session text: fixed title per category, signal codes, files and source positions only."""
    return {
        "id": c.id,
        "rank": c.rank,
        "turn": c.turn,
        "title": labels.CATEGORY_TITLE[c.category],
        "category": c.category,
        "category_label": labels.CATEGORY[c.category],
        "signals": [{"code": code, "label": labels.SIGNALS[code]} for code in c.signals],
        "files": c.files,
        "lines": [ws.session.events[idx].line for idx in c.edit_idxs],
        "evidence_level": c.evidence_level,
        "evidence_level_label": labels.EVIDENCE_LEVEL[c.evidence_level],
        "has_test_record": c.has_test_record,
        "retro_available": c.retro.eligible,
    }


def upload_view(ws: Workspace, expires_in_sec: int) -> dict[str, Any]:
    return {
        "scope": build_scope(ws.session, ws.test_runs),
        "candidates": [card(ws, c) for c in ws.candidates],
        "expires_in_sec": expires_in_sec,
    }


def retro_view(ws: Workspace) -> dict[str, Any] | None:
    c = selected(ws)
    if c is None:
        return None
    retro = c.retro
    if not retro.eligible:
        reason_label = labels.RETRO_REASON[retro.reason]
        return {
            "available": False,
            "reason": retro.reason,
            "reason_label": reason_label,
            "message": f"이 후보는 기록만으로 테스트 결과를 확정할 수 없어 예측 없이 분석으로 넘어갑니다. 이유: {reason_label}",
        }
    if ws.prediction is None:
        return {
            "available": True,
            "precontext": precontext(ws, c),
            "question": RETRO_QUESTION,
            "choices": [{"value": value, "label": label} for value, label in labels.PREDICTION_CHOICE.items()],
        }
    p = ws.prediction
    return {
        "available": True,
        "submitted": True,
        "prediction": {"choice": p["choice"], "choice_label": labels.PREDICTION_CHOICE[p["choice"]],
                       "reason": p["reason"]},
        "reveal": reveal(retro.target, ws.evidence),
        "label": p["label"],
        "label_text": labels.PREDICTION_LABEL[p["label"]],
        "checkpoints": checkpoints(retro.target),
        "note": PREDICTION_NOTE,
    }


def evidence_view(ws: Workspace, ev: Evidence, later_files: set[str] | None = None) -> dict[str, Any]:
    if later_files is None:
        later_files = _later_files(ws)
    return {
        "id": ev.id,
        "kind": ev.kind,
        "kind_label": labels.EVIDENCE_KIND[ev.kind],
        "turn": ev.turn,
        "line": ev.line,
        "file": ev.file,
        "text": ev.excerpt,
        "diff": diff_lines(ev) if ev.kind == "code_edit" else None,
        "later_fix": ev.kind == "code_edit" and ev.file in later_files,
    }


def _later_files(ws: Workspace) -> set[str]:
    """Files of the selected candidate edited again after the analysis window (the later_fix tag); empty while
    blinded, since a later fix hints at the result. One session scan per response, never one per item."""
    c = selected(ws)
    if c is None or not c.later_fix or _blinded(ws, c):
        return set()
    end = analysis_window(c, ws.session.events)[1]
    return {file for e in ws.session.events[end + 1 :] if (file := edit_file(e)) in c.files}


def visible_evidence(ws: Workspace) -> list[dict[str, Any]]:
    later_files = _later_files(ws)
    return [evidence_view(ws, ws.evidence[i], later_files) for i in visible_evidence_ids(ws)]


def select_view(ws: Workspace) -> dict[str, Any]:
    return {
        "selected": ws.selected,
        "tier": ws.tier,
        "tier_label": labels.TIER[ws.tier],
        "retro": retro_view(ws),
        "evidence": visible_evidence(ws),
    }


def prediction_view(ws: Workspace) -> dict[str, Any]:
    """The only response that carries the reveal (§4.5); later reads get the stored copy through retro_view."""
    retro = retro_view(ws)
    return {
        **{key: retro[key] for key in ("prediction", "reveal", "label", "label_text", "checkpoints", "note")},
        "evidence": visible_evidence(ws),
    }


def preview_view(record: dict[str, Any]) -> dict[str, Any]:
    """The stored preview of §5.1; its payload texts are already redacted and blinded."""
    return {**record, "stage_label": labels.STAGE[record["stage"]]}


def retrieval_result(state: str, retrieved: list[str], cited: list[str]) -> dict[str, Any]:
    return {"state": state, "state_label": labels.SEARCH_STATE[state], "retrieved": retrieved, "cited": cited}


def analysis_result(
    llm: LLMResult, sections: dict[str, list[dict[str, Any]]], stats: dict[str, int], bundle: Bundle,
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": llm.model,
        "mock": llm.mock,
        "decision_summary": llm.data["decision_summary"],
        "scope_note": llm.data["scope_note"],
        "sections": sections,
        "section_labels": dict(labels.SECTION),
        "stats": stats,
        "bundle_ids": [item.evidence_id for item in bundle.items],
        "bundle_chars": bundle.chars,
        "search_state": retrieval["state"],
        "search_state_label": retrieval["state_label"],
    }


def summary_result(out: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": out["model"],
        "mock": out["model"].startswith("mock-"),
        "summary": out["summary"],
        "elapsed_ms": out["elapsed_ms"],
        "label": SUMMARY_LABEL,
    }


def report_result(
    llm: LLMResult, sections: dict[str, Any], stats: dict[str, int], dropped_ids: int, bundle: Bundle
) -> dict[str, Any]:
    """The stored development-process report: graded statements (summary groups, finding observations, gaps),
    plain text fields (overview, not_determined, suggestions, actions) and citation stats like the analysis."""
    return {
        "model": llm.model,
        "mock": llm.mock,
        "overview": llm.data["overview"],
        "tasks": sections["tasks"],
        "changes": sections["changes"],
        "decisions": sections["decisions"],
        "tests": sections["tests"],
        "summary_labels": dict(labels.REPORT_SUMMARY),
        "not_determined": llm.data["not_determined"],
        "prompt_findings": sections["prompt_findings"],
        "gaps": sections["gaps"],
        "actions": sections["actions"],
        "dropped_id_count": dropped_ids,
        "stats": stats,
        "bundle_ids": [item.evidence_id for item in bundle.items],
        "bundle_chars": bundle.chars,
    }


def whatif_result(llm: LLMResult, tier: str, data: dict[str, Any], dropped_ids: int) -> dict[str, Any]:
    """`data` is the schema-checked What-if; `dropped_ids` counts related ids that were not in the sent bundle."""
    return {
        "model": llm.model,
        "mock": llm.mock,
        "tier": tier,
        "tier_label": labels.TIER[tier],
        **data,
        "dropped_id_count": dropped_ids,
        "question_labels": dict(labels.ANSWER_FIELD),
    }


def review_result(llm: LLMResult, points: list[dict[str, Any]]) -> dict[str, Any]:
    """`points` are the graded review points; a reply that failed the schema check (형식 미확인) keeps its raw text."""
    result = {
        "model": llm.model,
        "mock": llm.mock,
        "format_ok": llm.data is not None,
        "summary": llm.data["summary"] if llm.data is not None else "",
        "points": points,
    }
    if llm.data is None:
        result["text"] = llm.text
    return result


def feedback_result(
    attempt: int, llm: LLMResult, answers: dict[str, str], items: list[dict[str, Any]],
    instruction_check: dict[str, Any], comparison: list[dict[str, Any]], next_difficulty: dict[str, str] | None,
) -> dict[str, Any]:
    """`items` are the schema-checked feedback items (their ids already limited to the sent bundle)."""
    axes = llm.data["axes"]
    return {
        "attempt": attempt,
        "model": llm.model,
        "mock": llm.mock,
        "answers": answers,
        "items": [{**item, "field_label": labels.ANSWER_FIELD[item["field"]]} for item in items],
        "instruction_review": llm.data["instruction_review"],
        "instruction_check": instruction_check,
        "axes": axes,
        "axes_labels": {axis: labels.AXIS_EVIDENCE[value] for axis, value in axes.items()},
        "comparison": comparison,
        "coaching": llm.data["coaching"],
        "explanation": llm.data["explanation"],
        "next_difficulty": next_difficulty,
    }


def job_view(job: Job, *, with_result: bool = True) -> dict[str, Any]:
    view = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "status_label": labels.JOB_STATUS[job.status],
        "progress": job.progress,
        "model": job.model,
        "mock": bool(job.model and job.model.startswith("mock-")),
        "result": job.result,
        "error": job.error,
        "updated_at": job.updated_at,
    }
    if not with_result:
        del view["result"]
    return view


def workspace_view(ws: Workspace, expires_in_sec: int) -> dict[str, Any]:
    c = selected(ws)
    # Results only exist after the prediction; the check keeps a pipeline bug from leaking them while blinded.
    results = ws.results if c is not None and not _blinded(ws, c) else {}
    evidence = visible_evidence(ws)  # every visible item, so a page reload needs one request
    return {
        **upload_view(ws, expires_in_sec),
        "selected": ws.selected,
        "tier": ws.tier,
        "tier_label": labels.TIER[ws.tier] if ws.tier else None,
        "self_assessment": ws.self_assessment,
        "retro": retro_view(ws),
        "evidence_ids": [item["id"] for item in evidence],
        "evidence": evidence,
        "consents": {stage: {"providers": record.get("providers", {})} for stage, record in ws.consents.items()},
        "results": {**{key: results.get(key) for key in RESULT_KEYS}, "feedback": results.get("feedback") or []},
        "jobs": [job_view(job, with_result=False) for job in ws.jobs.values()],
        "calls": dict(ws.calls),
    }
