"""Job pipelines run by web/jobs.py (ARCHITECTURE §5.9): analysis, local LLM summary, What-if, feedback and the
independent reviews.

The routes check every precondition (selection, prediction gate, consent, earlier results); these functions assume
an unblinded workspace. Each pipeline runs consent.check_send right before its provider call and stores its result
in ws.results only on success, so a failed step keeps every earlier result.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from functools import partial
from typing import Any

from agency_loop.config import Settings
from agency_loop.decisions.retro import analysis_window
from agency_loop.flow import compare, instruction, prompts, retrieval, selfassess
from agency_loop.flow.bundle import analysis_bundle, candidate_section, unblinded_candidate
from agency_loop.flow.caps import Caps
from agency_loop.flow.citations import grade_points, grade_statements
from agency_loop.flow.consent import check_send, stage_payload
from agency_loop.flow.report import grade_report, review_gaps
from agency_loop.flow.schemas import ANALYSIS_SECTIONS, ANSWER_FIELDS, BY_STAGE
from agency_loop.model import Job, Workspace
from agency_loop.providers.base import Gate, LLMResult, ProviderError, Providers
from agency_loop.web import views
from agency_loop.web.jobs import update

# A What-if the model could not generate keeps only its reason: no task reaches the user.
NO_TASK = {
    "title": "", "scenario": "", "hypothetical_changes": [], "related_evidence_ids": [],
    "questions": dict.fromkeys(ANSWER_FIELDS, ""),
}
_PROGRESS = {
    "analysis": "심층 분석 요청 중",
    "report": "개발 과정 리포트 요청 중",
    "whatif": "가정 상황 생성 요청 중",
    "feedback1": "첫 피드백 요청 중",
    "feedback2": "최종 피드백 요청 중",
    "review:hcx": "HCX-007 독립 검토 요청 중",
    "review:deepseek": "DeepSeek 독립 검토 요청 중",
}
REVIEWERS = ("hcx", "deepseek")  # also their order in the comparison, after Claude


async def analysis(job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps) -> dict[str, Any]:
    """Ⓐ: related-request search -> bundle -> Claude -> citation grading. Stores "retrieval" and "analysis"."""
    with caps.analysis_slot():
        gate = caps.gate(ws, "analysis")
        if "retrieval" not in ws.results:  # a re-run reuses it: embedding and reranking are capped at 1 per flow
            update(job, "running", "관련 요청 검색 중")
            ws.results["retrieval"] = await _search(ws, settings, providers, gate)
        bundle = analysis_bundle(ws, settings)
        text = prompts.analysis_user(bundle, candidate_section(ws))
        check_send(ws, "analysis", "anthropic", bundle.items, text)
        with _no_doomed_retry(caps, ws, "anthropic", "analysis"):
            llm = await _claude(job, providers, settings, "analysis", text, gate)
        update(job, "validating", "인용 확인 중")
        sections: dict[str, list[dict[str, Any]]] = {}
        stats: dict[str, int] = {}
        for name in ANALYSIS_SECTIONS:
            sections[name], counts = grade_statements(llm.data[name], bundle.items)
            stats = {key: stats.get(key, 0) + n for key, n in counts.items()}
        ws.results["analysis"] = views.analysis_result(llm, sections, stats, bundle, ws.results["retrieval"])
        return ws.results["analysis"]


async def report(job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps) -> dict[str, Any]:
    """Ⓔ: the development-process report (session summary, prompt quality findings, review gaps and actions) on the
    same bundle as Ⓐ, under the analysis consent — after the retro prediction gate (§4.5), like Ⓐ itself. Only the
    stored gap points are reused by the What-if and feedback prompts; the rest of the report never reaches a model."""
    with caps.analysis_slot():
        gate = caps.gate(ws, "report")
        bundle = analysis_bundle(ws, settings)
        text = prompts.report_user(bundle, candidate_section(ws))
        check_send(ws, "analysis", "anthropic", bundle.items, text)
        with _no_doomed_retry(caps, ws, "anthropic", "report"):
            llm = await _claude(job, providers, settings, "report", text, gate)
        update(job, "validating", "인용 확인 중")
        sections, stats, dropped = grade_report(llm.data, bundle.items)
        ws.results["report"] = views.report_result(llm, sections, stats, dropped, bundle)
        return ws.results["report"]


async def summary(job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps) -> dict[str, Any]:
    """Local LLM context summary, for display only: it never enters a bundle or another model's input."""
    try:
        with caps.local_llm_slot():
            items, text = stage_payload(ws, "analysis", "local_llm", settings)
            check_send(ws, "analysis", "local_llm", items, text)
            update(job, "running", "Local LLM 요약 중")
            with _no_doomed_retry(caps, ws, "local_llm", "analysis"):
                out = await providers.summarizer.summarize(text, gate=caps.gate(ws, "analysis"))
    except ProviderError as exc:
        ws.results["summary"] = {"error": exc.to_dict()}
        raise
    job.model = out["model"]
    ws.results["summary"] = views.summary_result(out)
    return ws.results["summary"]


async def whatif(job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps) -> dict[str, Any]:
    """Ⓑ under the analysis consent: the consented bundle plus the tier code, never the raw self-assessment. The
    stored report's gap summary goes along when a report was stored (the route settles the report first)."""
    bundle = analysis_bundle(ws, settings)
    gaps = review_gaps(ws, {item.evidence_id for item in bundle.items})
    text = prompts.whatif_user(bundle, candidate_section(ws), ws.tier, gaps=gaps or None)
    check_send(ws, "analysis", "anthropic", bundle.items, text)
    with _no_doomed_retry(caps, ws, "anthropic", "whatif"):
        llm = await _claude(job, providers, settings, "whatif", text, caps.gate(ws, "whatif"))
    data = llm.data if llm.data["can_generate"] else {**llm.data, **NO_TASK}
    sent = {item.evidence_id for item in bundle.items}
    related = [i for i in data["related_evidence_ids"] if i in sent]
    dropped = len(data["related_evidence_ids"]) - len(related)
    ws.results["whatif"] = views.whatif_result(llm, ws.tier, {**data, "related_evidence_ids": related}, dropped)
    return ws.results["whatif"]


async def feedback(
    job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps, attempt: int,
    answers: dict[str, str],
) -> dict[str, Any]:
    """Ⓒ/Ⓓ on the consented answers, then the rule engine: instruction check, self-rating comparison and, for
    attempt 2, the next difficulty. Appends to ws.results["feedback"]."""
    stage = f"feedback{attempt}"
    items, text = stage_payload(ws, stage, "anthropic", settings, answers=answers)
    check_send(ws, stage, "anthropic", items, text)
    with _no_doomed_retry(caps, ws, "anthropic", stage):
        llm = await _claude(job, providers, settings, stage, text, caps.gate(ws, stage))
    # The UI links every id to an evidence card: keep only evidence that was sent.
    sent = {item.evidence_id for item in items if item.evidence_id in ws.evidence}
    checked = [{**item, "evidence_ids": [i for i in item["evidence_ids"] if i in sent]} for item in llm.data["items"]]
    window = analysis_window(unblinded_candidate(ws), ws.session.events)
    axes = llm.data["axes"]
    entry = views.feedback_result(
        attempt, llm, answers, checked,
        instruction.check_instruction(answers["ai_instruction"], ws.test_runs, window),
        selfassess.compare_axes(ws.self_assessment, axes),
        selfassess.next_difficulty(ws.tier, axes) if attempt == 2 else None,
    )
    ws.results.setdefault("feedback", []).append(entry)
    return entry


async def review(
    job: Job, ws: Workspace, settings: Settings, providers: Providers, caps: Caps, provider: str
) -> dict[str, Any]:
    """HCX-007 or DeepSeek on the consented analysis bundle, never Claude's output. Stores results["reviews"][provider]
    and recomputes results["comparison"] over Claude and every finished review."""
    stage = f"review:{provider}"
    items, text = stage_payload(ws, stage, provider, settings)
    check_send(ws, stage, provider, items, text)
    update(job, "running", _PROGRESS[stage])
    with _no_doomed_retry(caps, ws, provider, stage):
        llm = await getattr(providers, provider).review(
            system=prompts.SYSTEM["review"], user=text, schema=BY_STAGE["review"], gate=caps.gate(ws, stage),
            on_status=partial(update, job),
        )
    job.model = llm.model
    points = grade_points(llm.data["points"], items) if llm.data is not None else []
    reviews = ws.results.setdefault("reviews", {})
    reviews[provider] = views.review_result(llm, points)
    statements = [s for section in ws.results["analysis"]["sections"].values() for s in section]
    ws.results["comparison"] = compare.compare(
        {"anthropic": statements, **{name: reviews[name]["points"] for name in REVIEWERS if name in reviews}}
    )
    return reviews[provider]


async def _search(ws: Workspace, settings: Settings, providers: Providers, gate: Gate) -> dict[str, Any]:
    """The retrieval result; a provider or consent failure is a failed search and the analysis goes on without it."""
    try:
        found = await retrieval.retrieve(ws, settings, providers.embedder, gate)
        state, retrieved, cited = found["state"], found["retrieved"], []
        if retrieved:
            chosen = await retrieval.rerank(ws, providers.reranker, gate, retrieved)
            if chosen == []:  # the reranker ran and cited nothing
                state, retrieved = "no_results", []
            cited = chosen or []
    except ProviderError:
        state, retrieved, cited = "failed", [], []
    return views.retrieval_result(state, retrieved, cited)


@contextlib.contextmanager
def _no_doomed_retry(caps: Caps, ws: Workspace, provider: str, stage: str) -> Iterator[None]:
    """A failure stays retryable only while the per-flow cap still leaves a call for the retry."""
    try:
        yield
    except ProviderError as exc:
        if exc.retryable and caps.exhausted(ws, provider, stage):
            exc.retryable = False
        raise


async def _claude(job: Job, providers: Providers, settings: Settings, stage: str, text: str, gate: Gate) -> LLMResult:
    update(job, "running", _PROGRESS[stage])
    llm = await providers.claude.generate(
        stage=stage, system=prompts.SYSTEM[stage], user=text, schema=BY_STAGE[stage],
        effort=settings.claude_effort[stage], max_tokens=settings.claude_max_tokens[stage], gate=gate,
        on_status=partial(update, job),
    )
    job.model = llm.model
    return llm
