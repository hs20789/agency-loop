"""Evidence bundle for model calls: tagged, escaped and capped text from visible evidence only (ARCHITECTURE §5.2).

Every function that reads session content for a model refuses (ValueError) before selection and while the workspace
is blinded (§4.5). Later fixes, thinking, predictions and summaries never enter a bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.decisions.evidence import evidence_id, visible_evidence_ids
from agency_loop.decisions.retro import analysis_window
from agency_loop.ingest.parser import TRUNCATION_MARK
from agency_loop.model import Candidate, Evidence, Workspace

TAG_NAMES = (
    "evidence", "session_evidence", "candidate", "tier", "gaps", "whatif", "answer", "first_feedback", "task",
)
# "<" or "</" before one of our tag names, also as a prefix (<tasks>) or in another case (<TASK>): models read loosely.
_TAG_START = re.compile(r"<(?=/?(?:" + "|".join(TAG_NAMES) + "))", re.IGNORECASE)

OUTPUT_HEAD, OUTPUT_TAIL = 500, 2000  # command outputs: summaries are at the end
ITEM_CHARS = 3000
SUMMARY_CHARS = 4000
_CUT = "\n[... 이하 생략 ...]"
CUT_MARKS = (TRUNCATION_MARK, _CUT)  # inserted by the app: never part of a verified quote
_OPEN, _CLOSE = "<session_evidence>\n", "</session_evidence>"
# Bundle order by kind; a command and its output share a group so the output follows its command.
_GROUP = {"user_request": 0, "assistant_text": 1, "code_edit": 2, "command": 3, "command_output": 3, "tool_other": 4}


@dataclass(frozen=True, slots=True)
class BundleItem:
    evidence_id: str  # evidence id, or a pseudo id for other sent text ("query", "whatif", "answer:<field>", ...)
    kind: str
    turn: int | None
    file: str | None
    text: str  # capped and escaped, exactly as sent: hashes and citation checks use this text
    sha256: str


@dataclass(frozen=True, slots=True)
class Bundle:
    items: list[BundleItem]
    text: str  # the <session_evidence> section
    chars: int


def escape_text(text: str) -> str:
    """Session or user text: `<evidence` -> `<\\evidence`, `</answer` -> `<\\/answer` for every tag name of §5.2."""
    return _TAG_START.sub(lambda _m: "<\\", text)


def escape_attr(value: object) -> str:
    return str(value).replace('"', "'").replace("<", "‹").replace(">", "›")


def escape_json(data: object) -> str:
    """JSON whose strings went through escape_text: no section tag inside, and still valid JSON."""

    def walk(value: object) -> object:
        if isinstance(value, str):
            return escape_text(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {escape_text(str(key)): walk(item) for key, item in value.items()}
        return value

    return json.dumps(walk(data), ensure_ascii=False)


def text_item(item_id: str, kind: str, text: str) -> BundleItem:
    return BundleItem(item_id, kind, None, None, text, hashlib.sha256(text.encode()).hexdigest())


def evidence_item(ev: Evidence) -> BundleItem:
    """Item caps of §5.2 (outputs: head 500 + tail 2,000; others 3,000), then escaping."""
    text = ev.excerpt
    if ev.kind == "command_output":
        if len(text) > OUTPUT_HEAD + OUTPUT_TAIL:
            text = text[:OUTPUT_HEAD] + TRUNCATION_MARK + text[-OUTPUT_TAIL:]
    elif len(text) > ITEM_CHARS:
        text = text[:ITEM_CHARS] + _CUT
    text = escape_text(text)
    return BundleItem(ev.id, ev.kind, ev.turn, ev.file, text, hashlib.sha256(text.encode()).hexdigest())


def unblinded_candidate(ws: Workspace) -> Candidate:
    """The selected candidate once its evidence may reach a model; ValueError before selection or while blinded."""
    candidate = next((c for c in ws.candidates if c.id == ws.selected), None)
    if candidate is None:
        raise ValueError("not_selected")
    if candidate.retro.eligible and ws.prediction is None:
        raise ValueError("blinded")
    return candidate


def retrieval_pool(ws: Workspace) -> list[str]:
    """Human requests outside the candidate's analysis window: the only items retrieval may add."""
    start, end = analysis_window(unblinded_candidate(ws), ws.session.events)
    return [ev.id for ev in ws.evidence.values() if ev.kind == "user_request" and not start <= ev.idx <= end]


def build_bundle(
    ws: Workspace, settings: Settings, *, retrieved: list[str] | None = None, cited: list[str] | None = None,
    maximal: bool = False,
) -> Bundle:
    """The window's visible evidence in §5.2 order, then retrieved pool requests (cited first), never over the cap.

    Ids in `retrieved` that are not pool requests (window items, later fixes, unknown ids) are ignored. `maximal`
    keeps every droppable item: a preview lists all that a capped call could send, with the texts it would send.
    ValueError when the cap cannot hold even the tags of the decision's evidence.
    """
    candidate = unblinded_candidate(ws)
    pool = set(retrieval_pool(ws))
    window = [evidence_item(ws.evidence[i]) for i in visible_evidence_ids(ws) if i not in pool]
    window.sort(key=lambda item: _GROUP[item.kind])  # stable: time order inside each group
    tool_other = [n for n, item in enumerate(window) if item.kind == "tool_other"]
    older = [n for n, item in enumerate(window) if item.kind == "command_output"][:-1]  # the newest output stays
    core = [item for n, item in enumerate(window) if n not in {*tool_other, *older}]
    if _size(core) > settings.bundle_max_chars:
        # Everything droppable goes, retrieval included, so the fit depends on the window alone and a preview made
        # before retrieval shows the very texts that are sent.
        return _assemble([(item, None) for item in _fit(ws, candidate, core, settings.bundle_max_chars)])
    found = [i for i in dict.fromkeys(retrieved or []) if i in pool]
    marks = set(cited or [])
    ordered = [i for i in found if i in marks] + [i for i in found if i not in marks]
    entries = [(item, None) for item in window] + [(evidence_item(ws.evidence[i]), i in marks) for i in ordered]
    if maximal:
        return _assemble(entries)

    # Drop order: tool_other, uncited retrieved (weakest first), older outputs (oldest first), cited retrieved
    # (weakest first). What is left is the core, which fits.
    weakest = range(len(entries) - 1, len(window) - 1, -1)
    order = tool_other + [n for n in weakest if not entries[n][1]] + older + [n for n in weakest if entries[n][1]]
    blocks = [_render(item, flag) for item, flag in entries]
    size = len(_OPEN) + len(_CLOSE) + sum(map(len, blocks))
    dropped = set()
    for n in order:
        if size <= settings.bundle_max_chars:
            break
        dropped.add(n)
        size -= len(blocks[n])
    return _assemble([entry for n, entry in enumerate(entries) if n not in dropped])


def analysis_bundle(ws: Workspace, settings: Settings) -> Bundle:
    """The bundle of every Claude call and review: the window plus the stored retrieval result (§5.1 "same bundle")."""
    retrieval = ws.results.get("retrieval") or {}
    return build_bundle(ws, settings, retrieved=retrieval.get("retrieved"), cited=retrieval.get("cited"))


def candidate_section(ws: Workspace) -> str:
    """`<candidate>` with category, files and turn; no session text besides the file names."""
    c = unblinded_candidate(ws)
    data = {"category": c.category, "category_label": labels.CATEGORY[c.category], "files": c.files, "turn": c.turn}
    return f"<candidate>{escape_json(data)}</candidate>"


def summary_text(ws: Workspace) -> str:
    """Plain-text local LLM input: requests, AI explanations and code edits of the window (<= 4,000 chars)."""
    unblinded_candidate(ws)
    pool = set(retrieval_pool(ws))
    window = [ws.evidence[i] for i in visible_evidence_ids(ws) if i not in pool]
    parts = [
        f"[{labels.EVIDENCE_KIND[ev.kind]}{' ' + ev.file if ev.file else ''}]\n{ev.excerpt}"
        for kind in ("user_request", "assistant_text", "code_edit")
        for ev in window
        if ev.kind == kind
    ]
    return "\n\n".join(parts)[:SUMMARY_CHARS]


def search_query(ws: Workspace) -> BundleItem:
    """Retrieval query for the embedder and the reranker: the candidate's request, category and files."""
    c = unblinded_candidate(ws)
    request = evidence_item(ws.evidence[evidence_id(c.request_idx)])
    text = "\n".join([request.text, labels.CATEGORY[c.category], escape_text(", ".join(c.files))])
    return text_item("query", "query", text)


def documents_text(items: list[BundleItem]) -> str:
    """Payload text of the search providers: every sent text under its id."""
    return "\n\n".join(f"[{item.evidence_id}]\n{item.text}" for item in items)


def _size(items: list[BundleItem]) -> int:
    return len(_OPEN) + len(_CLOSE) + sum(len(_render(item, None)) for item in items)


def _assemble(entries: list[tuple[BundleItem, bool | None]]) -> Bundle:
    text = _OPEN + "".join(_render(item, flag) for item, flag in entries) + _CLOSE
    return Bundle([item for item, _ in entries], text, len(text))


def _fit(ws: Workspace, candidate: Candidate, items: list[BundleItem], cap: int) -> list[BundleItem]:
    """Window items that exceed the cap with nothing droppable left: drop the oldest explanations, edits, commands,
    outputs and in-between requests, keeping the first and last request, the decision edit and the target run, then
    cut every remaining text to one common length (citations are checked against the cut text that is sent)."""
    requests = [item.evidence_id for item in items if item.kind == "user_request"]
    run = candidate.retro.target
    runs = (run.use_idx, run.result_idx) if run else ()
    keep = {*requests[:1], *requests[-1:], *(evidence_id(i) for i in (candidate.decision_idx, *runs) if i is not None)}
    size = _size(items)
    dropped = set()
    for item in sorted(items, key=lambda item: ws.evidence[item.evidence_id].idx):  # oldest first
        if size <= cap:
            break
        if item.evidence_id not in keep:
            dropped.add(item.evidence_id)
            size -= len(_render(item, None))
    kept = [item for item in items if item.evidence_id not in dropped]
    if size <= cap:
        return kept
    lengths = [len(item.text) for item in kept]
    limit = _common_length(lengths, cap - size + sum(lengths))  # the room left for texts after wrapper and tags
    if limit < len(TRUNCATION_MARK):
        raise ValueError("bundle_max_chars")
    return [_cut(item, limit) for item in kept]


def _common_length(lengths: list[int], room: int) -> int:
    """The largest L with sum(min(n, L)) <= room: shorter texts stay whole, longer ones are cut to L."""
    left = len(lengths)
    for n in sorted(lengths):
        if n * left > room:
            return room // left
        room -= n
        left -= 1
    return max(lengths, default=0)


def _cut(item: BundleItem, limit: int) -> BundleItem:
    """The item with its escaped text cut to `limit` chars, marker included; outputs keep mostly the tail. A cut
    cannot form a section tag: tag starts in the text are escaped, and a "<" left at the end of the head is followed
    by the marker's newline."""
    text = item.text
    if len(text) <= limit:
        return item
    if item.kind == "command_output":
        room = limit - len(TRUNCATION_MARK)
        text = text[: room // 5] + TRUNCATION_MARK + text[len(text) - (room - room // 5) :]
    else:
        text = text[: limit - len(_CUT)] + _CUT
    return replace(item, text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


def _render(item: BundleItem, cited: bool | None) -> str:
    attrs = f'id="{item.evidence_id}" kind="{item.kind}" turn="{item.turn}"'
    if item.file:
        attrs += f' file="{escape_attr(item.file)}"'
    if cited is not None:  # retrieved requests only: true = chosen by the reranker
        attrs += f' cited="{"true" if cited else "false"}"'
    return f"<evidence {attrs}>\n{item.text}\n</evidence>\n"
