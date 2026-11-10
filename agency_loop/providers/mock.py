"""Deterministic mock providers (docs/ARCHITECTURE.md §6.3). No network; a small artificial delay.

They read only the tagged prompt sections of §5.2, cite real evidence ids and copy exact substrings of the sent
evidence text as quotes, so the app's citation check verifies them. Feedback is generic, rule-based on simple answer
features (length, conditions, tests) and never judges ability or gives scores.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import zlib

from agency_loop import labels
from agency_loop.flow.schemas import ANSWER_FIELDS, is_valid
from agency_loop.providers.base import Gate, LLMResult, ProviderError, Status

_EVIDENCE = re.compile(r"<evidence\b([^>]*)>(.*?)</evidence>", re.S)  # text inside is escaped by the bundle
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_ANSWER = re.compile(r'<answer field="(\w+)">(.*?)</answer>', re.S)
_TESTS = re.compile(r"테스트|검증|확인|실행|test|assert", re.I)
_CONDITIONS = re.compile(r"경우|조건|만약|때|예외|오류|에러|실패|\bif\b|\bwhen\b|error|fail", re.I)
_REQUIREMENTS = re.compile(r"해야|하도록|하지 말|반드시|요구|조건|\bmust\b|\bshould\b|\bensure\b", re.I)
_DIM = 256

_CHANGES = {
    "basic": ["입력값 하나가 비어 있는 상황"],
    "standard": ["외부 응답이 평소보다 늦게 오는 상황", "같은 요청이 연달아 두 번 들어오는 상황"],
    "advanced": [
        "외부 응답이 늦고 일부 필드가 빠진 상황",
        "재시도 중 같은 요청이 겹치는 상황",
        "처리 도중 실패해 일부 결과만 남은 상황",
    ],
}
_HINTS = {
    "expected_behavior": "가정한 조건에서 입력과 출력이 어떻게 달라지는지 한 가지 예로 적어 보세요.",
    "potential_issue": "문제가 생기는 조건과 그때 사용자가 보게 될 결과를 함께 적어 보세요.",
    "ai_instruction": "지켜야 할 조건과 작업 후 실행할 테스트를 지시에 넣어 보세요.",
    "verification": "어떤 테스트나 명령을 실행하고 어떤 결과면 통과인지 적어 보세요.",
}
_REVIEW_MODELS = {"hcx": "mock-hcx-007", "deepseek": "mock-deepseek-v4-1-flash-1017"}


def _split(user: str) -> tuple[list[dict], str]:
    """Evidence items as {"id", "kind", "file", ..., "text"} in prompt order, and the sections after the evidence.

    The bundle escapes our tag names in session text (<\\tier>, <\\/evidence>) and quotes and angle brackets in
    attribute values, so session text can neither close the evidence block nor pose as a later section.
    """
    evidence, _, sections = user.partition("</session_evidence>")
    items = []
    for m in _EVIDENCE.finditer(evidence):
        attrs = dict(_ATTR.findall(m.group(1)))
        if "id" in attrs:
            items.append({**attrs, "text": m.group(2)})
    return items, sections


def _first(items: list[dict], kind: str) -> dict | None:
    return next((i for i in items if i.get("kind") == kind), None)


def _section(sections: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", sections, re.S)
    return m.group(1).strip() if m else ""


def _json_section(sections: str, tag: str) -> dict:
    try:
        data = json.loads(_section(sections, tag))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _quote(item: dict | None) -> str:
    """An exact substring of the sent evidence text: the first added diff line, else the first non-empty line."""
    if item is None:
        return ""
    lines = [line.strip() for line in item["text"].splitlines() if line.strip()]
    added = [line for line in lines if line.startswith("+ ")]
    return (added or lines or [""])[0][:80].strip()


def _statement(text: str, item: dict | None, *, record: bool = False) -> dict:
    quote = _quote(item) if record else ""
    if item and quote:
        return {"text": text, "kind": "record", "evidence_ids": [item["id"]], "quote": quote}
    if item:
        return {"text": text, "kind": "interpretation", "evidence_ids": [item["id"]], "quote": ""}
    return {"text": text, "kind": "needs_verification", "evidence_ids": [], "quote": ""}


def _analysis(items: list[dict], sections: str) -> dict:
    request, explanation = _first(items, "user_request"), _first(items, "assistant_text")
    edit, command = _first(items, "code_edit"), _first(items, "command")
    target = f"{edit['file']} 변경" if edit and edit.get("file") else "이 변경"
    if command:
        check = _statement("실행한 명령이 기록되어 있어 같은 명령으로 다시 확인할 수 있습니다.", command, record=True)
    else:
        check = _statement("검증 명령 기록이 없어 확인 방법을 직접 정해야 합니다.", None)
    return {
        "decision_summary": f"[모의 응답] {target}에 담긴 결정을 근거 {len(items)}개로 정리했습니다.",
        "reasons": [
            _statement("사용자 요청에 이 변경의 출발점이 기록되어 있습니다.", request, record=True),
            _statement("AI 설명에서 이 방식을 고른 이유를 짐작할 수 있습니다.", explanation),
        ],
        "alternatives": [_statement("다른 방식을 비교한 기록은 근거 묶음에서 확인되지 않습니다.", None)],
        "assumptions": [_statement("입력과 실행 환경이 예상대로라는 가정에 기대는 변경일 수 있습니다.", edit)],
        "risks": [_statement("추가된 코드가 예외 상황까지 다루는지 확인이 필요합니다.", edit, record=True)],
        "verification_points": [check],
        "scope_note": "모의 응답(개발용)입니다. 실제 모델 분석이 아니며 근거 묶음의 항목만 인용했습니다.",
    }


def _report(items: list[dict], sections: str) -> dict:
    """[모의 응답] The development-process report fixture: schema-valid, real evidence ids, exact-substring quotes."""
    request = _first(items, "user_request")
    explanation = _first(items, "assistant_text")
    edit, command = _first(items, "code_edit"), _first(items, "command")
    short = bool(request) and len(request["text"].strip()) < 40
    tasks = [_statement("사용자 요청이 이 개발의 출발점입니다.", request, record=True)] if request else []
    changes = [_statement("이 결정의 코드 변경이 기록되어 있습니다.", edit, record=True)] if edit else []
    decisions = []
    if explanation:
        decisions = [_statement("AI 설명에 이 구현의 의도가 드러납니다.", explanation)]
    if command:
        tests = [_statement("명령 실행 기록이 있어 같은 명령으로 다시 확인할 수 있습니다.", command, record=True)]
    elif any(i.get("kind") == "command_output" for i in items):
        tests = [_statement("명령 출력이 기록되어 있습니다.", _first(items, "command_output"), record=True)]
    else:
        tests = [_statement("테스트나 명령 실행 기록이 근거에서 확인되지 않습니다.", None)]
    findings = []
    if request:
        findings.append({
            "aspect": "clarity",
            "observation": _statement(
                "요청 문구가 짧아 조건과 확인 방법이 드러나지 않습니다." if short
                else "요청 문구에서 일부 조건을 확인할 수 있습니다.",
                request, record=True,
            ),
            "suggestion": "[모의 응답] 지켜야 할 조건과 확인 방법을 요청에 함께 적어 두는 제안 예시입니다.",
        })
    gaps = [{
        "concern": "missing_verification",
        "text": "[모의 응답] 추가된 코드의 예외 상황을 확인한 기록이 근거에서 확인되지 않습니다"
                "(기록에 없다는 뜻은 아닙니다).",
        "kind": "interpretation",
        "evidence_ids": [edit["id"]] if edit else [],
        "quote": "",
    }]
    actions = [{
        "kind": "ask_ai",
        "text": "[모의 응답] AI에게 이 변경이 다루지 않는 예외 상황을 확인해 보라고 요청해 보는 제안 예시입니다.",
        "evidence_ids": [edit["id"]] if edit else [],
    }]
    if command:
        actions.append({
            "kind": "tests",
            "text": "[모의 응답] 기록에 있는 명령으로 확인 절차를 한 번 더 실행해 보라는 제안 예시입니다.",
            "evidence_ids": [command["id"]],
        })
    return {
        "overview": f"[모의 응답] 근거 {len(items)}개에서 확인되는 요청과 변경·실행 기록을 개발 흐름으로 정리한 "
                    "예시입니다. 실제 모델 분석이 아닙니다.",
        "tasks": tasks,
        "changes": changes,
        "decisions": decisions,
        "tests": tests,
        "not_determined": [
            "[모의 응답] 근거 묶음 밖의 대화와 이후 수정 기록은 보내지 않아 확인할 수 없습니다.",
            "[모의 응답] 세션 개발자가 실제로 어떤 확인을 했는지는 기록만으로 알 수 없습니다.",
        ],
        "prompt_findings": findings,
        "gaps": gaps,
        "actions": actions,
    }


def _whatif(items: list[dict], sections: str) -> dict:
    tier, edit = _section(sections, "tier"), _first(items, "code_edit")
    if edit is None or tier not in _CHANGES:
        return {
            "can_generate": False,
            "reason": "[모의 응답] 코드 변경 근거나 난이도 정보가 없어 가정 상황을 만들지 않았습니다.",
            "title": "",
            "scenario": "",
            "hypothetical_changes": [],
            "related_evidence_ids": [],
            "questions": dict.fromkeys(ANSWER_FIELDS, ""),
        }
    changes, file = _CHANGES[tier], edit.get("file") or "변경된 코드"
    return {
        "can_generate": True,
        "reason": "",
        "title": f"[모의 응답] {file}: {changes[0]}이라면",
        "scenario": f"실제 기록의 {file} 변경을 바탕으로 {'과 '.join(changes)}이 생겼다고 가정합니다. "
        "실제로 일어난 일이 아닌 가정형 상황입니다.",
        "hypothetical_changes": changes,
        "related_evidence_ids": [i["id"] for i in items if i.get("kind") in ("user_request", "code_edit")],
        "questions": {
            "expected_behavior": "이 가정에서 코드는 어떻게 동작할 것이라고 예상하나요?",
            "potential_issue": "이때 어떤 문제가 생길 수 있나요?",
            "ai_instruction": "AI에게 이 상황을 처리하도록 어떻게 지시하겠습니까?",
            "verification": "그 처리가 맞는지 어떻게 확인하겠습니까?",
        },
    }


def _feedback(items: list[dict], sections: str, *, second: bool = False) -> dict:
    answers = dict(_ANSWER.findall(sections))
    known = {i["id"] for i in items}
    ids = [i for i in _json_section(sections, "whatif").get("related_evidence_ids", []) if i in known][:2]
    edit = _first(items, "code_edit")
    ids = ids or ([edit["id"]] if edit else [])
    entries = []
    for field in ANSWER_FIELDS:
        answer = answers.get(field, "").strip()
        checks = [
            (len(answer) >= 30, "상황을 자기 말로 구체적으로 적었습니다.", "답변이 짧아 판단 과정이 잘 드러나지 않습니다."),
            (bool(_CONDITIONS.search(answer)), "조건이나 예외 상황을 짚었습니다.", "어떤 조건에서 결과가 달라지는지 드러나지 않습니다."),
        ]
        if field in ("ai_instruction", "verification"):
            checks.append(
                (bool(_TESTS.search(answer)), "테스트나 확인 절차를 언급했습니다.", "결과를 확인할 테스트나 절차가 빠져 있습니다.")
            )
        gaps = [gap for ok, _, gap in checks if not ok]
        entries.append(
            {
                "field": field,
                "strengths": [good for ok, good, _ in checks if ok],
                "gaps": gaps,
                "hint": _HINTS[field] if gaps else "근거 기록과 한 번 더 대조해 보세요.",
                "evidence_ids": ids,
            }
        )

    def axis(*fields: str) -> str:
        chosen = [e for e in entries if e["field"] in fields]
        if not any(e["gaps"] for e in chosen):
            return "shown"
        return "partial" if any(e["strengths"] for e in chosen) else "not_shown"

    gap_fields = [labels.ANSWER_FIELD[e["field"]] for e in entries if e["gaps"]]
    explanation = ""
    if second:
        first = _json_section(sections, "first_feedback").get("items", [])
        before = {e.get("field"): len(e.get("gaps", [])) for e in first if isinstance(e, dict)}
        fewer = [labels.ANSWER_FIELD[e["field"]] for e in entries if len(e["gaps"]) < before.get(e["field"], 0)]
        explanation = "[모의 응답] 첫 피드백과 비교해 " + (
            f"{', '.join(fewer)} 항목의 보완할 점이 줄었습니다." if fewer else "보완할 점의 수는 그대로입니다."
        )
    instruction = answers.get("ai_instruction", "")
    return {
        "items": entries,
        "instruction_review": {
            "has_requirements": bool(_REQUIREMENTS.search(instruction)),
            "has_verification_steps": bool(_TESTS.search(instruction)),
            "note": "[모의 응답] 지시에 요구 조건과 확인 절차가 있는지 단순 규칙으로만 살폈습니다.",
        },
        "axes": {
            "understanding": axis("expected_behavior", "potential_issue"),
            "verification": axis("verification"),
            "control": axis("ai_instruction"),
        },
        "coaching": "[모의 응답] 답변에 드러난 표현만 단순 규칙으로 살핀 일반 안내이며 능력을 판정하지 않습니다. "
        + (f"{', '.join(gap_fields)} 항목부터 보완해 보세요." if gap_fields else "네 항목 모두 따로 보완할 점이 보이지 않습니다."),
        "explanation": explanation,
    }


def _review(items: list[dict], name: str) -> dict:
    edit = _first(items, "code_edit")
    # The two reviewers cite partly different evidence so the comparison view shows both groups.
    other = _first(items, "command" if name == "deepseek" else "user_request")
    points = [
        {
            "type": "issue",
            "text": "추가된 코드가 예외 상황까지 다루는지 근거만으로는 확인되지 않습니다.",
            "evidence_ids": [edit["id"]] if edit else [],
            "quote": _quote(edit),
        },
        {
            "type": "judgment" if other else "missing_evidence",
            "text": "이 기록이 결정의 배경을 보여 줍니다." if other else "판단에 필요한 기록이 근거 묶음에 없습니다.",
            "evidence_ids": [other["id"]] if other else [],
            "quote": _quote(other),
        },
    ]
    summary = f"[모의 응답] {labels.PROVIDER[name]} 독립 검토 형식의 예시이며 실제 모델 의견이 아닙니다."
    return {"summary": summary, "points": points}


def _result(provider: str, model: str, data: dict, schema: dict, stop_reason: str, started: float,
            on_status: Status | None) -> LLMResult:
    if on_status:
        on_status("validating", "응답 형식 확인 중")
    if not is_valid(data, schema):  # mock and schema drifted apart: fail like a live adapter would
        raise ProviderError("invalid_output", attempted=True)
    return LLMResult(
        provider=provider,
        model=model,
        data=data,
        text=json.dumps(data, ensure_ascii=False),
        stop_reason=stop_reason,
        attempts=1,
        mock=True,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


_STAGES = {
    "analysis": _analysis,
    "report": _report,
    "whatif": _whatif,
    "feedback1": _feedback,
    "feedback2": lambda items, sections: _feedback(items, sections, second=True),
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _bag_of_words(text: str) -> list[float]:
    vec = [0.0] * _DIM
    for token in _tokens(text):
        vec[zlib.crc32(token.encode()) % _DIM] += 1.0  # crc32, not hash(): stable across processes
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class MockClaude:
    model = "mock-claude-opus-5-5"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def generate(
        self, *, stage: str, system: str, user: str, schema: dict, effort: str, max_tokens: int,
        gate: Gate, on_status: Status | None = None,
    ) -> LLMResult:
        build = _STAGES.get(stage)
        if build is None:
            raise ProviderError("bad_request")
        gate("anthropic")
        started = time.monotonic()
        await asyncio.sleep(self.delay)
        if on_status:
            on_status("streaming", "응답 수신 중")
        return _result("anthropic", self.model, build(*_split(user)), schema, "end_turn", started, on_status)


class MockReviewer:
    def __init__(self, name: str, delay: float = 0.05) -> None:
        self.name = name  # "hcx" | "deepseek"
        self.model = _REVIEW_MODELS[name]
        self.delay = delay

    async def review(
        self, *, system: str, user: str, schema: dict, gate: Gate, on_status: Status | None = None
    ) -> LLMResult:
        gate(self.name)
        started = time.monotonic()
        await asyncio.sleep(self.delay)
        return _result(self.name, self.model, _review(_split(user)[0], self.name), schema, "stop", started, on_status)


class MockEmbedder:
    model = "mock-bge-m3"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def embed(self, texts: list[str], *, gate: Gate) -> list[list[float]]:
        if not texts:
            return []
        gate("local_rag")
        await asyncio.sleep(self.delay)
        return [_bag_of_words(text) for text in texts]


class MockReranker:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def rerank(self, query: str, documents: list[dict], *, gate: Gate) -> list[str]:
        """Top 2 documents ({"id", "doc"}) by word overlap with the query; no overlap is never cited."""
        docs = documents[:10]
        if not docs:
            return []
        gate("clova_rerank")
        await asyncio.sleep(self.delay)
        words = set(_tokens(query))
        overlaps = [len(words & set(_tokens(d["doc"]))) for d in docs]
        ranked = sorted(range(len(docs)), key=lambda i: -overlaps[i])  # stable: ties keep input order
        return [docs[i]["id"] for i in ranked[:2] if overlaps[i] > 0]


class MockSummarizer:
    model = "mock-local-llm"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def summarize(self, text: str, *, max_sentences: int = 3, gate: Gate) -> dict:
        gate("local_llm")
        started = time.monotonic()
        await asyncio.sleep(self.delay)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text[:4000]) if s.strip()]
        return {
            "model": self.model,
            "summary": " ".join(sentences[:max_sentences]),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
