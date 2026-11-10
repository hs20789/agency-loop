"""Shared data model. Every module codes against these types (see docs/ARCHITECTURE.md §2).

Change a field only together with every module and test that uses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["user_text", "assistant_text", "tool_use", "tool_result"]
EvidenceKind = Literal[
    "user_request", "assistant_text", "code_edit", "command", "command_output", "tool_other"
]
Category = Literal[
    "error_handling", "input_validation", "external_call", "dependency_config", "assumption", "design_choice"
]
EvidenceLevel = Literal["sufficient", "partial", "insufficient"]
TestResult = Literal["pass", "fail"]
RetroReason = Literal[
    "no_test", "unsupported_runner", "code_changed", "no_exit_code", "truncated", "interrupted", "inconsistent"
]


@dataclass(frozen=True, slots=True)
class Event:
    """One allowlisted, redacted unit of the session, in file order."""

    idx: int  # 0-based position in Session.events (file order = time order)
    line: int  # 1-based JSONL line number in the uploaded file (source position)
    kind: EventKind
    turn: int  # 1-based human-request number; 0 = before the first human request
    text: str = ""  # redacted user/assistant text or tool_result output; "" for tool_use
    tool: str | None = None  # tool name on tool_use and tool_result ("Bash", "Edit", "Write", ...)
    tool_use_id: str | None = None  # remapped link id "tu1", "tu2", ... (original ids are dropped)
    tool_input: dict[str, Any] | None = None  # redacted allowlisted input (ARCHITECTURE §3.2)
    is_error: bool | None = None  # tool_result only
    interrupted: bool | None = None  # tool_result only


@dataclass(slots=True)
class ParseReport:
    total_lines: int = 0
    parsed_records: int = 0
    bad_lines: list[int] = field(default_factory=list)  # line numbers only, never content
    skipped_record_types: dict[str, int] = field(default_factory=dict)  # e.g. {"summary": 1}
    dropped: dict[str, int] = field(default_factory=dict)  # e.g. {"thinking": 12, "image": 1, "meta_fields": 40}


@dataclass(slots=True)
class RedactionStats:
    # category -> count. Categories: secret, email, ip, home_path, phone, url_credential, private_key
    in_text: dict[str, int] = field(default_factory=dict)  # user/assistant text and tool inputs
    in_outputs: dict[str, int] = field(default_factory=dict)  # tool_result outputs (hidden until reveal)


@dataclass(slots=True)
class Session:
    events: list[Event]
    report: ParseReport
    redaction: RedactionStats


@dataclass(frozen=True, slots=True)
class Evidence:
    """One evidence item per event. id = "E%04d" % event.idx."""

    id: str
    idx: int
    line: int
    turn: int
    kind: EvidenceKind
    file: str | None  # code_edit: edited file (redacted, cwd-relative when possible)
    excerpt: str  # redacted text, capped by Settings.evidence_excerpt_chars


@dataclass(frozen=True, slots=True)
class TestRun:
    use_idx: int  # Bash tool_use event idx
    result_idx: int | None  # matching tool_result event idx, None when missing
    command: str  # redacted command text
    runner: str | None  # "pytest" | "unittest" | "jest"; None = test-like but unsupported
    exit_code: int | None  # explicit "Exit code N", or 0 inferred from is_error=False; None if unknown
    exit_source: str | None  # "explicit" | "is_error_false" | None
    result: TestResult | None  # rule-engine verdict; None when not determinable
    summary_line: str | None  # runner summary line used for the verdict (redacted)
    complete: bool  # summary present and no truncation marker
    interrupted: bool


@dataclass(slots=True)
class RetroInfo:
    eligible: bool
    reason: RetroReason | None = None  # set when not eligible
    target: TestRun | None = None  # SERVER ONLY. Never serialized before the prediction is stored.


@dataclass(slots=True)
class Candidate:
    id: str  # "c1".."c3" in rank order
    rank: int
    turn: int
    request_idx: int | None  # human request event that opened the unit
    edit_idxs: list[int]  # code-edit tool_use idxs in the unit (the decision)
    decision_idx: int  # last edit idx of the unit
    files: list[str]
    category: Category
    signals: list[str]  # result-independent signal codes (labels.SIGNALS)
    score: float
    evidence_ids: list[str]  # pre-decision evidence of the unit (idx <= decision_idx)
    evidence_level: EvidenceLevel
    has_test_record: bool  # a test-like command exists after decision_idx
    retro: RetroInfo
    later_fix: bool = False  # an edit to the same files exists after the analysis window


@dataclass(slots=True)
class Job:
    id: str
    kind: str  # "analysis" | "summary" | "review:hcx" | "review:deepseek" | "whatif" | "feedback1" | "feedback2"
    status: str = "queued"  # queued | running | streaming | validating | done | failed
    progress: str = ""  # short Korean status text
    model: str | None = None  # actual model name reported by the provider
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None  # {"code", "message", "retryable"}
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class Workspace:
    id: str  # 256-bit random token; cookie only, never in URLs or logs
    created_at: float
    last_used: float
    session: Session
    evidence: dict[str, Evidence]
    test_runs: list[TestRun]
    candidates: list[Candidate]
    selected: str | None = None  # candidate id; selection locks the workspace to that candidate
    self_assessment: dict[str, int] | None = None  # {"understanding": 0-2, "verification": 0-2, "control": 0-2}
    tier: str | None = None  # "basic" | "standard" | "advanced"
    prediction: dict[str, Any] | None = None  # {"choice", "reason", "label", "submitted_at"}
    previews: dict[str, dict[str, Any]] = field(default_factory=dict)  # preview_id -> preview record
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)  # stage -> consent record
    results: dict[str, Any] = field(default_factory=dict)  # "analysis", "summary", "retrieval", "reviews", "whatif", "feedback"
    jobs: dict[str, Job] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)  # per-flow attempt counters by provider
