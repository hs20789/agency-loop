"""Starlette app factory and routes (ARCHITECTURE §7.2): upload, workspace, selection, evidence, prediction,
previews and consent, and the analysis, summary retry, independent review, What-if and feedback jobs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.decisions import retro
from agency_loop.decisions.candidates import analyze_session
from agency_loop.decisions.evidence import visible_evidence_ids
from agency_loop.flow import orchestrator, selfassess
from agency_loop.flow.bundle import unblinded_candidate
from agency_loop.flow.caps import Caps
from agency_loop.flow.consent import consented, make_preview, record_consent
from agency_loop.ingest.parser import parse_session
from agency_loop.model import Candidate, Job, Workspace
from agency_loop.providers.base import ProviderError, Providers
from agency_loop.providers.registry import build_providers
from agency_loop.web import views
from agency_loop.web.jobs import JobRunner
from agency_loop.web.security import COOKIE, SecurityMiddleware, clear_workspace_cookie, set_workspace_cookie
from agency_loop.web.upload import TooLarge, read_body
from agency_loop.web.workspace import WorkspaceStore

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# The one synthetic practice session behind 시연 파일로 바로 해 보기: a fixed path, nothing from the client picks a file.
DEMO_SESSION = Path(__file__).resolve().parents[2] / "fixtures" / "sessions" / "demo_weather.jsonl"
JSON_MAX_BYTES = 64 * 1024
SWEEP_INTERVAL_SEC = 60
REASON_MAX_CHARS = 1000


def create_app(settings: Settings | None = None, providers: Providers | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    jobs = JobRunner()
    store = WorkspaceStore(settings, jobs)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        sweeper = asyncio.create_task(_sweep_forever(store))
        try:
            yield
        finally:
            sweeper.cancel()

    app = Starlette(
        routes=ROUTES,
        middleware=[Middleware(SecurityMiddleware, allowed_hosts=settings.allowed_hosts)],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs
    app.state.providers = providers if providers is not None else build_providers(settings)
    app.state.caps = Caps(settings)  # one per app: the daily totals are server-wide
    return app


async def _sweep_forever(store: WorkspaceStore) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SEC)
        store.sweep()


def _workspace(request: Request):
    return request.app.state.store.get(request.cookies.get(COOKIE))


async def _json(request: Request) -> Any:
    """Parsed JSON body (capped), or None when it is too large, not JSON, or holds a lone surrogate."""
    try:
        value = json.loads(await read_body(request, JSON_MAX_BYTES))
        # A "\ud800" escape parses but cannot be encoded: stored as a reason or answer it breaks every later response.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except (TooLarge, ValueError, RecursionError):  # UnicodeEncodeError is a ValueError
        return None


async def index(_request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def demo_session(_request: Request) -> Response:
    if not DEMO_SESSION.is_file():  # an install without the repository's fixtures
        return views.error("not_found", 404)
    return FileResponse(DEMO_SESSION, media_type="application/x-ndjson", filename=DEMO_SESSION.name)


async def health(request: Request) -> Response:
    return JSONResponse(views.health_view(request.app.state.settings))


async def create_workspace(request: Request) -> Response:
    settings, store = request.app.state.settings, request.app.state.store
    if not store.allow_upload(request.client.host if request.client else ""):
        return views.error("rate_limited", 429)
    try:
        body = await read_body(request, settings.max_upload_bytes)
    except TooLarge:
        return views.error("too_large", 413)
    try:  # parsing up to 32 MiB takes seconds of CPU: keep it off the event loop
        session = await run_in_threadpool(parse_session, body)
    except ValueError:
        return views.error("bad_format", 400)
    if not session.events:
        return views.error("no_events", 422)
    evidence, test_runs, candidates = await run_in_threadpool(analyze_session, session, settings)
    ws = store.add(request.cookies.get(COOKIE), session, evidence, test_runs, candidates)
    if ws is None:
        return views.error("server_busy", 503)
    response = JSONResponse(views.upload_view(ws, settings.workspace_ttl_sec), status_code=201)
    set_workspace_cookie(response, ws.id)
    return response


async def get_workspace(request: Request) -> Response:
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    # Every access refreshes the sliding TTL, so the full TTL remains.
    return JSONResponse(views.workspace_view(ws, request.app.state.settings.workspace_ttl_sec))


async def delete_workspace(request: Request) -> Response:
    request.app.state.store.remove(request.cookies.get(COOKIE))
    response = Response(status_code=204)
    clear_workspace_cookie(response)
    return response


async def select(request: Request) -> Response:
    # Read the body first: nothing below awaits, so the lock check and the lock itself are atomic.
    body = await _json(request)
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if ws.selected is not None:
        return views.error("locked", 409)
    if not isinstance(body, dict):
        return views.error("invalid_request", 400)
    try:
        sa = selfassess.validate(body.get("self_assessment"))
    except ValueError:
        return views.error("invalid_request", 400)
    candidate = next((c for c in ws.candidates if c.id == body.get("candidate_id")), None)
    if candidate is None:
        return views.error("not_found", 404)
    ws.selected, ws.self_assessment, ws.tier = candidate.id, sa, selfassess.tier_for(sa)
    return JSONResponse(views.select_view(ws))


async def evidence(request: Request) -> Response:
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    evidence_id = request.path_params["evidence_id"]
    if evidence_id not in visible_evidence_ids(ws):
        return views.error("not_found", 404)
    return JSONResponse(views.evidence_view(ws, ws.evidence[evidence_id]))


async def job(request: Request) -> Response:
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    found = ws.jobs.get(request.path_params["job_id"])
    if found is None:
        return views.error("not_found", 404)
    return JSONResponse(views.job_view(found))


async def prediction(request: Request) -> Response:
    body = await _json(request)  # read first: nothing below awaits, so the check and the store are atomic
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    candidate = views.selected(ws)
    if candidate is None:
        return views.error("precondition", 409)
    if not candidate.retro.eligible:
        return views.error("not_eligible", 409)
    if ws.prediction is not None:
        return views.error("already_submitted", 409)
    if not isinstance(body, dict):
        return views.error("invalid_request", 400)
    choice, reason = body.get("choice"), body.get("reason", "")
    if not isinstance(choice, str) or choice not in labels.PREDICTION_CHOICE:
        return views.error("invalid_request", 400)
    if not isinstance(reason, str) or len(reason) > REASON_MAX_CHARS:
        return views.error("invalid_request", 400)
    label = retro.label(choice, candidate.retro.target.result)
    # Kept on the workspace only: bundles, previews and prompts never read it.
    ws.prediction = {"choice": choice, "reason": reason, "label": label, "submitted_at": time.time()}
    return JSONResponse(views.prediction_view(ws))


async def preview(request: Request) -> Response:
    body = await _json(request)
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None:
        return views.error("precondition", 409)
    if not isinstance(body, dict) or not isinstance(body.get("stage"), str):
        return views.error("invalid_request", 400)
    state = request.app.state
    try:
        record = make_preview(ws, body["stage"], state.settings, state.providers, answers=body.get("answers"))
    except ValueError:  # unknown stage or invalid answers
        return views.error("invalid_request", 400)
    except ProviderError as exc:  # an earlier result is missing, or nothing may be sent (insufficient evidence)
        return views.error(exc.code, 409, exc.message)
    return JSONResponse(views.preview_view(record))


async def consent(request: Request) -> Response:
    body = await _json(request)
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None:
        return views.error("precondition", 409)
    if not isinstance(body, dict):
        return views.error("invalid_request", 400)
    try:
        return JSONResponse(record_consent(ws, body.get("preview_id"), body.get("providers")))
    except KeyError:  # unknown, replaced or another workspace's preview
        return views.error("not_found", 404)
    except ValueError:
        return views.error("invalid_request", 400)


async def analysis(request: Request) -> Response:
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    candidate = _unblinded(ws)
    if candidate is None:
        return views.error("precondition", 409)
    if candidate.evidence_level == "insufficient":  # rule-engine evidence only: no provider at all, Local LLM included
        ws.results["retrieval"] = views.retrieval_result("insufficient", [], [])  # search state "근거 부족"
        return JSONResponse({"job_id": None, "summary_job_id": None, "notice": labels.ERROR["insufficient_evidence"]})
    if _active(ws, "analysis"):
        return views.error("already_submitted", 409)
    notice = None if consented(ws, "analysis", "anthropic") else labels.ERROR["consent_missing"]
    started = None if notice else _start(request, ws, "analysis", orchestrator.analysis)
    # The summary runs on its own and never feeds the analysis; the per-flow cap allows one real local LLM call.
    summary = None
    if consented(ws, "analysis", "local_llm") and not _active(ws, "summary"):
        summary = _start(request, ws, "summary", orchestrator.summary)
    return JSONResponse({
        "job_id": started.id if started else None,
        "summary_job_id": summary.id if summary else None,
        "notice": notice,
    })


async def report(request: Request) -> Response:
    """Ⓔ The development-process report (개발 세션 요약·프롬프트 품질·보완 포인트·개선 행동) after the analysis,
    under the analysis consent: one job per flow unless it failed (the per-flow Claude cap counts every call)."""
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    candidate = _unblinded(ws)
    if candidate is None:
        return views.error("precondition", 409)
    if candidate.evidence_level == "insufficient":  # rule-engine evidence only, like the analysis: no provider
        return JSONResponse({"job_id": None, "notice": labels.ERROR["insufficient_evidence"]})
    if not ws.results.get("analysis"):
        return views.error("precondition", 409)  # the report reuses the analysis bundle; the analysis goes first
    if not consented(ws, "analysis", "anthropic"):
        return JSONResponse({"job_id": None, "notice": labels.ERROR["consent_missing"]})
    if _active(ws, "report"):
        return views.error("already_submitted", 409)
    return JSONResponse({"job_id": _start(request, ws, "report", orchestrator.report).id, "notice": None})


async def summary_retry(request: Request) -> Response:
    """A new Local LLM summary after a failed one, under the analysis consent; the per-flow cap still counts."""
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None or not any(j.kind == "summary" for j in ws.jobs.values()):
        return views.error("precondition", 409)  # the first summary starts with POST /api/analysis
    if not consented(ws, "analysis", "local_llm"):
        return views.error("consent_missing", 409)
    if _active(ws, "summary"):
        return views.error("already_submitted", 409)
    return JSONResponse({"job_id": _start(request, ws, "summary", orchestrator.summary).id})


async def review(request: Request) -> Response:
    """HCX-007 or DeepSeek after a done analysis, under the consent of its own stage; one per provider unless the
    previous one failed (the per-flow call cap still counts)."""
    body = await _json(request)  # read first: nothing below awaits, so the check and the job start are atomic
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None or not ws.results.get("analysis"):
        return views.error("precondition", 409)
    provider = body.get("provider") if isinstance(body, dict) else None
    if provider not in orchestrator.REVIEWERS:
        return views.error("invalid_request", 400)
    stage = f"review:{provider}"
    if not consented(ws, stage, provider):
        return views.error("consent_missing", 409)
    if _active(ws, stage):
        return views.error("already_submitted", 409)
    return JSONResponse({"job_id": _start(request, ws, stage, orchestrator.review, provider).id})


async def whatif(request: Request) -> Response:
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None or not ws.results.get("analysis"):
        return views.error("precondition", 409)
    if _in_flight(ws, "report"):  # the report's gap summary informs the scenario: let it settle first
        return views.error("precondition", 409)
    if not consented(ws, "analysis", "anthropic"):  # the What-if sends the analysis bundle under its consent
        return views.error("consent_missing", 409)
    if _active(ws, "whatif"):
        return views.error("already_submitted", 409)
    return JSONResponse({"job_id": _start(request, ws, "whatif", orchestrator.whatif).id})


async def feedback(request: Request) -> Response:
    body = await _json(request)
    ws = _workspace(request)
    if ws is None:
        return views.error("no_workspace", 404)
    if _unblinded(ws) is None:
        return views.error("precondition", 409)
    attempt = body.get("attempt") if isinstance(body, dict) else None
    if type(attempt) is not int or attempt < 1 or not isinstance(body.get("preview_id"), str):
        return views.error("invalid_request", 400)
    stage, done = f"feedback{attempt}", ws.results.get("feedback") or []
    if attempt > 2 or len(done) >= attempt or _active(ws, stage):
        return views.error("already_submitted", 409)
    if not (ws.results.get("whatif") or {}).get("can_generate") or len(done) < attempt - 1:
        return views.error("precondition", 409)
    record = ws.consents.get(stage) or {}
    if record.get("preview_id") != body["preview_id"] or not consented(ws, stage, "anthropic"):
        return views.error("consent_missing", 409)
    # The answers are the ones the user saw in the consented preview, never the request body.
    started = _start(request, ws, stage, orchestrator.feedback, attempt, record["answers"])
    return JSONResponse({"job_id": started.id})


def _unblinded(ws: Workspace) -> Candidate | None:
    """The selected candidate once model-bound steps may run; None before selection or while blinded (§4.5)."""
    try:
        return unblinded_candidate(ws)
    except ValueError:
        return None


def _active(ws: Workspace, kind: str) -> bool:
    """A job of this kind that has not failed: a new one may start only after a failure."""
    return any(j.kind == kind and j.status != "failed" for j in ws.jobs.values())


def _in_flight(ws: Workspace, kind: str) -> bool:
    """A job of this kind that has neither finished nor failed: the next step waits for it to settle."""
    return any(j.kind == kind and j.status not in ("done", "failed") for j in ws.jobs.values())


def _start(request: Request, ws: Workspace, kind: str, run: Any, *args: Any) -> Job:
    # Keep only the newest attempt per kind, so repeated retries cannot grow the workspace; caps count every call.
    for job_id in [j.id for j in ws.jobs.values() if j.kind == kind and j.status == "failed"]:
        del ws.jobs[job_id]
    state = request.app.state
    return state.jobs.start(ws, kind, lambda job: run(job, ws, state.settings, state.providers, state.caps, *args))


ROUTES = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/demo-session", demo_session),
    Route("/api/workspaces", create_workspace, methods=["POST"]),
    Route("/api/workspace", get_workspace, methods=["GET"]),
    Route("/api/workspace", delete_workspace, methods=["DELETE"]),
    Route("/api/select", select, methods=["POST"]),
    Route("/api/evidence/{evidence_id}", evidence),
    Route("/api/prediction", prediction, methods=["POST"]),
    Route("/api/preview", preview, methods=["POST"]),
    Route("/api/consent", consent, methods=["POST"]),
    Route("/api/analysis", analysis, methods=["POST"]),
    Route("/api/report", report, methods=["POST"]),
    Route("/api/summary", summary_retry, methods=["POST"]),
    Route("/api/reviews", review, methods=["POST"]),
    Route("/api/whatif", whatif, methods=["POST"]),
    Route("/api/feedback", feedback, methods=["POST"]),
    Route("/api/jobs/{job_id}", job),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]
