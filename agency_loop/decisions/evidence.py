"""Evidence items, diff rendering and the evidence blinding gate (docs/ARCHITECTURE.md §4.1, §4.5)."""

from __future__ import annotations

from agency_loop.ingest.scope import EDIT_TOOLS
from agency_loop.model import Event, Evidence, EvidenceKind, Session, Workspace


def evidence_id(idx: int) -> str:
    return f"E{idx:04d}"


def edit_file(event: Event) -> str | None:
    """File of an edit tool_use; None for every other event."""
    if event.kind != "tool_use" or event.tool not in EDIT_TOOLS:
        return None
    tool_input = event.tool_input or {}
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def edit_hunks(event: Event) -> list[tuple[str, str]]:
    """(old, new) text pairs of an edit tool_use; Write and NotebookEdit have no old text."""
    tool_input = event.tool_input or {}
    if event.tool == "MultiEdit":
        return [(edit.get("old_string", ""), edit.get("new_string", "")) for edit in tool_input.get("edits", [])]
    if event.tool == "Edit":
        return [(tool_input.get("old_string", ""), tool_input.get("new_string", ""))]
    return [("", tool_input.get("content" if event.tool == "Write" else "new_source", ""))]


def build_evidence(session: Session, excerpt_chars: int) -> dict[str, Evidence]:
    """One evidence item per event, keyed by id in event order."""
    evidence = {}
    for event in session.events:
        kind, text = _kind_and_text(event)
        item = Evidence(
            id=evidence_id(event.idx),
            idx=event.idx,
            line=event.line,
            turn=event.turn,
            kind=kind,
            file=edit_file(event),
            excerpt=_cap(text, excerpt_chars, tail=event.kind == "tool_result"),
        )
        evidence[item.id] = item
    return evidence


def diff_lines(evidence: Evidence) -> list[dict[str, str]]:
    """Viewer rows of a code_edit excerpt ("@" = file header or hunk separator); [] for other kinds."""
    if evidence.kind != "code_edit":
        return []
    rows = []
    for line in evidence.excerpt.split("\n"):
        if line[:2] in ("- ", "+ "):
            rows.append({"op": line[0], "text": line[2:]})
        elif line == "@@" or line.startswith("--- "):
            rows.append({"op": "@", "text": line})
        else:
            rows.append({"op": " ", "text": line})
    return rows


def visible_evidence_ids(ws: Workspace) -> list[str]:
    """The single evidence blinding gate (§4.5): ids that responses, previews and providers may use now."""
    from agency_loop.decisions.retro import analysis_window, precontext  # retro imports this module

    candidate = next((c for c in ws.candidates if c.id == ws.selected), None)
    if candidate is None:
        return []
    if candidate.retro.eligible and ws.prediction is None:
        pre = precontext(ws, candidate)
        ids = {pre["request_id"], pre["last_request_id"], *pre["edit_ids"], pre["command"]["evidence_id"]} - {None}
    else:
        start, end = analysis_window(candidate, ws.session.events)
        ids = {evidence_id(idx) for idx in range(start, end + 1)}
        # Retrieved ids come from a model pipeline: only human requests may widen the window.
        retrieved = (ws.results.get("retrieval") or {}).get("retrieved") or []
        ids.update(i for i in retrieved if i in ws.evidence and ws.evidence[i].kind == "user_request")
    return sorted(ids, key=lambda i: ws.evidence[i].idx)


def _kind_and_text(event: Event) -> tuple[EvidenceKind, str]:
    if event.kind == "user_text":
        return "user_request", event.text
    if event.kind == "assistant_text":
        return "assistant_text", event.text
    if event.kind == "tool_result":
        return ("command_output" if event.tool == "Bash" else "tool_other"), event.text
    if event.tool in EDIT_TOOLS:
        return "code_edit", _diff_text(event)
    tool_input = event.tool_input or {}
    if event.tool == "Bash":
        return "command", tool_input.get("command", "")
    values = [value for value in tool_input.values() if isinstance(value, str)]
    return "tool_other", " ".join([event.tool or "", *values]).strip()


def _diff_text(event: Event) -> str:
    lines = [f"--- {edit_file(event) or ''}"]
    for number, (old, new) in enumerate(edit_hunks(event)):
        if number:
            lines.append("@@")
        lines += ["- " + line for line in old.splitlines()]
        lines += ["+ " + line for line in new.splitlines()]
    return "\n".join(lines)


def _cap(text: str, limit: int, *, tail: bool) -> str:
    """Cut to `limit` chars with an ellipsis; outputs keep the tail, where runner summaries are."""
    if len(text) <= limit:
        return text
    return "…" + text[len(text) - limit + 1 :] if tail else text[: limit - 1] + "…"
