"""Per-provider send previews, consent records and the last check before any send (ARCHITECTURE §5.1).

Previews refuse while blinded and list only visible evidence (plus, for the analysis stage, the retrieval pool).
A consent copies what the user saw (item hashes, answers), so a newer preview cannot widen it. Callers run
`check_send` right before a provider call: a declined provider is never called and its counters stay 0.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.decisions.evidence import visible_evidence_ids
from agency_loop.flow import prompts
from agency_loop.flow.bundle import (
    BundleItem,
    analysis_bundle,
    build_bundle,
    candidate_section,
    documents_text,
    evidence_item,
    retrieval_pool,
    search_query,
    summary_text,
    text_item,
    unblinded_candidate,
)
from agency_loop.flow.report import review_gaps
from agency_loop.flow.schemas import ANSWER_FIELDS
from agency_loop.ingest.redact import find_secrets
from agency_loop.model import Workspace
from agency_loop.providers.base import ProviderError, Providers

STAGE_PROVIDERS = {
    "analysis": ("local_rag", "clova_rerank", "local_llm", "anthropic"),
    "review:hcx": ("hcx",),
    "review:deepseek": ("deepseek",),
    "feedback1": ("anthropic",),
    "feedback2": ("anthropic",),
}
ANSWER_MAX_CHARS = 2000
MOCK_DESTINATION = "모의 응답(실제로 보내지 않음)"
OFF = "꺼져 있음"

# Confirmed facts only; everything else says 미확인.
_LOCAL_POLICY = ("수업 서버: 공인 IP 평문 HTTP, 인증 없음", "보관 여부 미확인")
POLICY = {
    "anthropic": ("API 데이터는 모델 학습에 쓰지 않음(공식 자료)", "30일 안에 삭제(공식 자료)"),
    "deepseek": ("Public Preview: 입력·출력이 학습에 쓰일 수 있음(약관 §4.3)", "학습 거부·ZDR 미적용", "보관 기간 미확인"),
    "hcx": ("데이터 정책 미확인",),
    "clova_rerank": ("데이터 정책 미확인",),
    "local_rag": _LOCAL_POLICY,
    "local_llm": _LOCAL_POLICY,
}
_CLIENT = {
    "anthropic": "claude", "hcx": "hcx", "deepseek": "deepseek",
    "local_rag": "embedder", "clova_rerank": "reranker", "local_llm": "summarizer",
}
_URL = {
    "anthropic": "anthropic_base_url", "hcx": "clova_base_url", "clova_rerank": "clova_base_url",
    "deepseek": "mistral_base_url", "local_rag": "ollama_embed_url", "local_llm": "ollama_chat_url",
}
_REVIEW_PURPOSE = "독립 검토. Claude의 분석 결과는 보내지 않습니다."
_PURPOSE = {
    "local_rag": "관련 요청 검색을 위한 임베딩. 검색 질의와 이 결정 범위 밖의 사람 요청을 보냅니다.",
    "clova_rerank": "검색된 관련 요청 중 인용할 근거 선택. 검색 질의와, 아래 요청 중 검색된 최대 10개만 보냅니다.",
    "local_llm": "화면 표시용 맥락 요약. 요약은 근거나 다른 모델의 입력으로 쓰지 않습니다.",
    "hcx": _REVIEW_PURPOSE,
    "deepseek": _REVIEW_PURPOSE,
    "feedback1": "답변에 대한 첫 피드백",
    "feedback2": "다시 제출한 답변에 대한 최종 피드백",
}


def validate_answers(value: Any) -> dict[str, str]:
    """Exactly the four answer fields, each a string of at most 2,000 chars. Raises ValueError otherwise."""
    if not isinstance(value, dict) or set(value) != set(ANSWER_FIELDS):
        raise ValueError("answers")
    if any(not isinstance(value[field], str) or len(value[field]) > ANSWER_MAX_CHARS for field in ANSWER_FIELDS):
        raise ValueError("answers")
    return {field: value[field] for field in ANSWER_FIELDS}


def stage_payload(
    ws: Workspace, stage: str, provider: str, settings: Settings, *, answers: dict[str, str] | None = None
) -> tuple[list[BundleItem], str]:
    """(items, text) for a provider at a stage. Exact for local_rag, local_llm, reviews and feedback, so callers send
    this very payload. For the analysis stage's reranker and Claude it is the maximal set; their calls send a subset.

    Raises ProviderError("precondition") when an earlier result the payload needs is missing.
    """
    if provider in ("local_rag", "clova_rerank"):
        items = [search_query(ws), *(evidence_item(ws.evidence[i]) for i in retrieval_pool(ws))]
        return items, documents_text(items)
    if provider == "local_llm":
        text = summary_text(ws)
        return [text_item("summary_input", "summary_input", text)], text
    candidate = candidate_section(ws)
    if stage == "analysis":
        enough = _human_requests(ws) >= settings.rag_min_requests
        # Every item a capped call could keep, with the (possibly cut) texts it would send.
        bundle = build_bundle(ws, settings, retrieved=retrieval_pool(ws) if enough else None, maximal=True)
        return bundle.items, prompts.analysis_user(bundle, candidate)
    if stage.startswith("review:"):
        if not ws.results.get("analysis"):
            raise ProviderError("precondition")
        bundle = analysis_bundle(ws, settings)
        return bundle.items, prompts.review_user(bundle, candidate)
    whatif = ws.results.get("whatif") or {}
    feedback = ws.results.get("feedback") or []
    if not whatif.get("can_generate") or (stage == "feedback2" and not feedback) or answers is None:
        raise ProviderError("precondition")
    first = feedback[0] if stage == "feedback2" else None
    bundle = analysis_bundle(ws, settings)
    gaps = review_gaps(ws, {item.evidence_id for item in bundle.items})
    sections = prompts.feedback_sections(whatif, answers, first, gaps=gaps or None)
    pseudo = [text_item(key, key.split(":")[0], text) for key, text in sections.items()]
    return [*bundle.items, *pseudo], prompts.feedback_user(bundle, candidate, whatif, answers, first, gaps=gaps or None)


def _destination(settings: Settings, name: str) -> str:
    if name == "local_llm" and settings.mcp_url:  # the HTTP MCP server is what receives the text
        return f"MCP {settings.mcp_url}"
    return getattr(settings, _URL[name]) or "주소 미설정"


def make_preview(
    ws: Workspace, stage: str, settings: Settings, providers: Providers, *, answers: Any = None
) -> dict[str, Any]:
    """Stores and returns `{preview_id, stage, providers: [...]}` (§5.1); feedback previews also keep `answers`.

    Raises ValueError for an unknown stage, invalid answers, no selection or a blinded workspace, and
    ProviderError("precondition") when the stage's earlier results are missing or, for the analysis stage, when the
    evidence level is insufficient (no provider runs then, so there is nothing to consent to). Older previews of the
    stage are dropped.
    """
    if stage not in STAGE_PROVIDERS:
        raise ValueError("stage")
    candidate = unblinded_candidate(ws)
    if stage == "analysis" and candidate.evidence_level == "insufficient":
        raise ProviderError("precondition", labels.ERROR["insufficient_evidence"])
    if stage.startswith("feedback"):
        answers = validate_answers(answers)
    else:
        answers = None
    entries = []
    for name in STAGE_PROVIDERS[stage]:
        mode = providers.modes.get(name, "off")
        skip = _skip_reason(ws, name, settings, getattr(providers, _CLIENT[name]))
        items, text = ([], "") if skip else stage_payload(ws, stage, name, settings, answers=answers)
        entries.append({
            "name": name,
            "label": labels.PROVIDER[name],
            "destination": MOCK_DESTINATION if mode == "mock" else _destination(settings, name),
            "purpose": _purpose(stage, name, settings),
            "mock": mode == "mock",
            "applicable": skip is None,
            "skip_reason": skip,
            "items": [{"evidence_id": i.evidence_id, "chars": len(i.text), "sha256": i.sha256} for i in items],
            "payload_text": text,
            "chars": len(text),
            "policy": list(POLICY[name]),
        })
    allowed = {*visible_evidence_ids(ws), *retrieval_pool(ws)}
    assert all(i["evidence_id"] in allowed for e in entries for i in e["items"] if i["evidence_id"] in ws.evidence)
    record: dict[str, Any] = {"preview_id": secrets.token_urlsafe(16), "stage": stage, "providers": entries}
    if answers is not None:
        record["answers"] = answers
    for old in [pid for pid, r in ws.previews.items() if r["stage"] == stage]:
        del ws.previews[old]  # bounds memory; consents keep their own copy
    ws.previews[record["preview_id"]] = record
    return record


def record_consent(ws: Workspace, preview_id: Any, decisions: Any) -> dict[str, Any]:
    """Stores ws.consents[stage] for a stored preview; returns `{"stage", "providers": {name: bool}}`.

    KeyError for an unknown preview, ValueError unless `decisions` maps the preview's provider names to bools.
    Missing names count as declined; a provider that is not applicable is never consented.
    """
    preview = ws.previews.get(preview_id) if isinstance(preview_id, str) else None
    if preview is None:
        raise KeyError("preview")
    entries = {e["name"]: e for e in preview["providers"]}
    if not isinstance(decisions, dict) or not set(decisions) <= set(entries):
        raise ValueError("providers")
    if any(type(value) is not bool for value in decisions.values()):
        raise ValueError("providers")
    chosen = {name: decisions.get(name, False) and entry["applicable"] for name, entry in entries.items()}
    ws.consents[preview["stage"]] = {
        "preview_id": preview_id,
        "providers": chosen,
        "items": {name: {i["evidence_id"]: i["sha256"] for i in entries[name]["items"]} for name in chosen if chosen[name]},
        "answers": preview.get("answers"),
    }
    return {"stage": preview["stage"], "providers": chosen}


def consented(ws: Workspace, stage: str, provider: str) -> bool:
    return bool((ws.consents.get(stage) or {}).get("providers", {}).get(provider))


def check_send(ws: Workspace, stage: str, provider: str, items: list[BundleItem], text: str) -> None:
    """Right before a provider call: consent, every item previewed with the same text, no secret in the payload."""
    consent = ws.consents.get(stage) or {}
    if not consent.get("providers", {}).get(provider):
        raise ProviderError("consent_missing")
    previewed = consent["items"].get(provider, {})
    # Hash the text again: an item's stored sha256 could be stale.
    if any(previewed.get(item.evidence_id) != hashlib.sha256(item.text.encode()).hexdigest() for item in items):
        raise ProviderError("precondition")
    if find_secrets(text):
        raise ProviderError("secret_detected")


def _human_requests(ws: Workspace) -> int:
    return sum(ev.kind == "user_request" for ev in ws.evidence.values())


def _skip_reason(ws: Workspace, name: str, settings: Settings, client: object) -> str | None:
    if client is None:
        return OFF
    if name in ("local_rag", "clova_rerank") and _human_requests(ws) < settings.rag_min_requests:
        return f"사람 요청이 {settings.rag_min_requests}개 미만이라 검색을 생략합니다"
    return None


def _purpose(stage: str, name: str, settings: Settings) -> str:
    if name != "anthropic":
        return _PURPOSE[name]
    if stage != "analysis":
        return _PURPOSE[stage]
    return (f"심층 분석, 개발 과정 분석 리포트, 가정 상황(What-if) 생성. 실제 호출에는 검색 결과에 따라 고른 일부만 "
            f"들어가며, 근거 묶음 상한은 {settings.bundle_max_chars:,}자입니다. 리포트가 만들어진 뒤에는 그 판단·검증 "
            f"보완 포인트 요약을 What-if와 피드백 생성에 함께 보냅니다.")
