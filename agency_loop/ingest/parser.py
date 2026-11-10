"""Claude Code session JSONL -> Session (docs/ARCHITECTURE.md §3.1, §3.2).

Only allowlisted fields are copied, and every kept string goes through the session's Redactor.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agency_loop.ingest.redact import Redactor
from agency_loop.model import Event, ParseReport, Session

TRUNCATION_MARK = "\n[... 출력 일부 생략 ...]\n"

_TOOL_FIELDS = {
    "Bash": ("command", "description"),
    "Edit": ("file_path", "old_string", "new_string", "replace_all"),
    "MultiEdit": ("file_path", "edits"),
    "Write": ("file_path", "content"),
    "NotebookEdit": ("notebook_path", "new_source"),
    "Read": ("file_path",),
    "Grep": ("pattern", "path"),
    "Glob": ("pattern", "path"),
}
_EDIT_FIELDS = ("old_string", "new_string", "replace_all")
_PATH_FIELDS = ("file_path", "notebook_path", "path")
_META_KEYS = {"uuid", "parentUuid", "sessionId", "cwd", "version", "gitBranch", "userType", "requestId", "timestamp"}
_MESSAGE_META_KEYS = {"id", "model", "usage"}
_SKIPPED_TYPES = ("summary", "system", "file-history-snapshot")
# Slash commands, their output, and `!` bash-mode input/output are not requests to the AI.
_WRAPPERS = (
    "<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>",
    "<local-command-caveat>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
)
_INTERRUPT = re.compile(r"\[Request interrupted by user[^\]\n]*\]")
_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL)
# json.loads keeps lone surrogate escapes ("\ud800"; valid pairs are joined) and no response could encode them.
_SURROGATE = re.compile("[\ud800-\udfff]")


def parse_session(data: bytes, *, max_output_chars: int = 200_000) -> Session:
    """Raises ValueError("bad_format") when no line is a JSON object; events may be empty."""
    lines = data.decode("utf-8-sig", errors="replace").split("\n")  # not splitlines(): JSON may hold raw U+2028
    if lines[-1] == "":
        lines.pop()
    builder = _Builder(ParseReport(total_lines=len(lines)), max_output_chars)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            record = None
        if isinstance(record, dict):
            builder.report.parsed_records += 1
            builder.add(number, record)
        else:
            builder.report.bad_lines.append(number)
    if not builder.report.parsed_records:
        raise ValueError("bad_format")
    return Session(events=builder.events, report=builder.report, redaction=builder.redactor.stats)


def _count(bucket: dict[str, int], key: str, n: int = 1) -> None:
    bucket[key] = bucket.get(key, 0) + n


class _Builder:
    def __init__(self, report: ParseReport, max_output_chars: int) -> None:
        self.report = report
        self.max_output_chars = max_output_chars
        self.redactor = Redactor()
        self.events: list[Event] = []
        self.turn = 0
        self.cwd = ""
        self.ids: dict[str, str] = {}  # original tool_use id -> "tuN"
        self.tools: dict[str, str | None] = {}  # "tuN" -> tool name

    def add(self, line: int, record: dict[str, Any]) -> None:
        if not self.cwd and isinstance(record.get("cwd"), str):
            self.cwd = record["cwd"].rstrip("/\\")
        message = record.get("message")
        if _META_KEYS.intersection(record) or (isinstance(message, dict) and _MESSAGE_META_KEYS.intersection(message)):
            _count(self.report.dropped, "meta_fields")
        kind = record.get("type")
        skipped = self.report.skipped_record_types
        if kind not in ("user", "assistant"):
            _count(skipped, kind if kind in _SKIPPED_TYPES else "other")
        elif record.get("isSidechain") is True:
            _count(skipped, "sidechain")
        elif kind == "user" and record.get("isMeta") is True:
            _count(skipped, "meta")
        elif kind == "user" and record.get("isCompactSummary") is True:  # /compact summary can reveal results
            _count(skipped, "summary")
        elif not isinstance(message, dict) or not isinstance(message.get("content"), str | list):
            _count(skipped, "other")
        elif kind == "user":
            self._user(line, message["content"], record.get("toolUseResult"))
        else:
            self._assistant(line, message["content"])

    def _redact(self, text: str, origin: str) -> str:
        """Every kept string passes here; a lone surrogate becomes U+FFFD like an invalid byte."""
        return self.redactor.redact(_SURROGATE.sub("\ufffd", text), origin=origin)

    def _emit(self, line: int, kind: str, **fields: Any) -> None:
        self.events.append(Event(idx=len(self.events), line=line, kind=kind, turn=self.turn, **fields))

    def _user(self, line: int, content: str | list, tool_use_result: Any) -> None:
        interrupted = tool_use_result.get("interrupted") if isinstance(tool_use_result, dict) else None
        texts = []
        for block in [{"type": "text", "text": content}] if isinstance(content, str) else content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and isinstance(block.get("text"), str):
                text = self._human_text(block["text"])
                if text:
                    texts.append(text)
            elif kind == "tool_result":
                self._tool_result(line, block, interrupted if isinstance(interrupted, bool) else None)
            else:
                _count(self.report.dropped, "image" if kind == "image" else "other")
        if texts:  # tool results above belong to the previous turn; this text opens the next one
            self.turn += 1
            self._emit(line, "user_text", text=self._redact("\n\n".join(texts), origin="text"))

    def _human_text(self, raw: str) -> str | None:
        text, removed = _REMINDER.subn("", raw)
        if removed:
            _count(self.report.dropped, "system_reminder", removed)
        text = text.strip()
        if text.startswith(_WRAPPERS):
            _count(self.report.dropped, "command_wrapper")
        elif _INTERRUPT.fullmatch(text):
            _count(self.report.dropped, "interrupt_marker")
        else:
            return text
        return None

    def _tool_result(self, line: int, block: dict[str, Any], interrupted: bool | None) -> None:
        tool_use_id = self._link(block.get("tool_use_id"))
        content = block.get("content")
        parts = [content] if isinstance(content, str) else []
        for item in content if isinstance(content, list) else []:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                _count(self.report.dropped, "image" if kind == "image" else "other")
        # Redact before cutting so a secret spanning the cut is still masked as a whole.
        output = self._redact("\n".join(parts), origin="output")
        if len(output) > self.max_output_chars:
            half = self.max_output_chars // 2
            output = output[:half] + TRUNCATION_MARK + output[len(output) - half :]
        is_error = block.get("is_error")
        self._emit(
            line, "tool_result", text=output, tool=self.tools.get(tool_use_id) if tool_use_id else None,
            tool_use_id=tool_use_id, is_error=is_error if isinstance(is_error, bool) else None, interrupted=interrupted,
        )

    def _assistant(self, line: int, content: str | list) -> None:
        for block in [{"type": "text", "text": content}] if isinstance(content, str) else content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    self._emit(line, "assistant_text", text=self._redact(block["text"].strip(), origin="text"))
            elif kind == "tool_use":
                self._tool_use(line, block)
            elif kind in ("thinking", "redacted_thinking"):
                _count(self.report.dropped, "thinking")
            else:
                _count(self.report.dropped, "other")

    def _tool_use(self, line: int, block: dict[str, Any]) -> None:
        name = block.get("name") if isinstance(block.get("name"), str) else None
        fields = _TOOL_FIELDS.get(name) if name else None
        raw = block.get("input") if isinstance(block.get("input"), dict) else {}
        if fields is None:
            _count(self.report.dropped, "other_tool_input")
        tool = self._redact(name, origin="text") if name else None
        tool_use_id = self._link(block.get("id"))
        if tool_use_id:
            self.tools[tool_use_id] = tool
        self._emit(line, "tool_use", tool=tool, tool_use_id=tool_use_id, tool_input=self._clean(raw, fields or ()))

    def _clean(self, raw: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        """Allowlisted, redacted copy of a tool input; values of an unexpected type are dropped."""
        out: dict[str, Any] = {}
        for key in fields:
            value = raw.get(key)
            if key == "edits":
                if isinstance(value, list):
                    out[key] = [self._clean(edit, _EDIT_FIELDS) for edit in value if isinstance(edit, dict)]
            elif key == "replace_all":
                if isinstance(value, bool):
                    out[key] = value
            elif isinstance(value, str):
                out[key] = self._redact(self._relative(value) if key in _PATH_FIELDS else value, origin="text")
        return out

    def _relative(self, path: str) -> str:
        cwd = self.cwd
        if cwd and path == cwd:
            return "."
        if cwd and path.startswith(cwd) and path[len(cwd) : len(cwd) + 1] in ("/", "\\"):
            return path[len(cwd) + 1 :]
        return path

    def _link(self, raw_id: Any) -> str | None:
        if not isinstance(raw_id, str) or not raw_id:
            return None
        return self.ids.setdefault(raw_id, f"tu{len(self.ids) + 1}")
