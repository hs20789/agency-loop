"""Decision candidates: request units, result-independent signals, ranking (docs/ARCHITECTURE.md §4.3).

Cards are shown before the prediction, so signals and ranking read only requests, AI text, edits and commands
of the unit up to its decision, never tool_result text.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.decisions.evidence import build_evidence, edit_file, edit_hunks, evidence_id
from agency_loop.decisions.retro import analysis_window, retro_info
from agency_loop.decisions.testruns import find_test_runs
from agency_loop.model import Candidate, Category, Event, Evidence, Session, TestRun

MAX_CANDIDATES = 3

WEIGHTS = {
    "try_except": 3,
    "raise_error": 2,
    "input_check": 2,
    "http_call": 3,
    "timeout_retry": 2,
    "file_or_db_io": 1,
    "dependency_change": 2,
    "config_change": 1,
    "assumption_language": 3,
    "alternative_language": 2,
    "new_function": 1,
    "large_change": 1,
}
_ADDED_LINE_SIGNALS = {
    "try_except": re.compile(r"^\s*(try:|except\b|finally:|catch\s*\(|\}\s*catch|\.catch\(|rescue\b)"),
    "raise_error": re.compile(r"\b(raise|throw)\b"),
    "input_check": re.compile(r"isinstance\(|\bif not \w|ValueError|TypeError|validat|\bassert\b|\.strip\(\)|len\(\w+\)\s*[<>=]"),
    "http_call": re.compile(r"requests\.|httpx\.|urllib|aiohttp|fetch\(|axios|urlopen|http\.client|websocket"),
    "timeout_retry": re.compile(r"timeout|retr(y|ies)|backoff|sleep\("),
    "file_or_db_io": re.compile(r"\bopen\(|Path\(|sqlite3|psycopg|cursor\.|\.execute\(|json\.(load|dump)|csv\."),
    "new_function": re.compile(r"^\s*(async def |def |class |function |export (async )?function |func |const \w+\s*=\s*(async )?\()"),
}
# Natural language, so case-insensitive ("Probably", "Instead of").
_TEXT_SIGNALS = {
    "assumption_language": re.compile(
        r"가정|아마|일반적으로|보통은|추정|것으로 보입니다|assum|probably|likely|should be fine|presumably", re.IGNORECASE
    ),
    "alternative_language": re.compile(
        r"대신|대안|선택|방식으로|instead of|rather than|alternative|trade-?off|chose|choose", re.IGNORECASE
    ),
}
_INSTALL = re.compile(r"pip install|uv add|poetry add|npm (install|i)\b|yarn add|pnpm add")
# Category groups in tie-break order.
_GROUPS: dict[Category, tuple[str, ...]] = {
    "error_handling": ("try_except", "raise_error"),
    "external_call": ("http_call", "timeout_retry", "file_or_db_io"),
    "input_validation": ("input_check",),
    "dependency_config": ("dependency_change", "config_change"),
    "assumption": ("assumption_language", "alternative_language"),
    "design_choice": ("new_function", "large_change"),
}
_LEVELS = ("insufficient", "partial", "sufficient")  # by the number of records present

_CODE_EXTENSIONS = frozenset(
    ".py .js .ts .tsx .jsx .mjs .cjs .java .kt .go .rs .rb .php .cs .c .h .cpp .hpp .swift .scala .sh .sql .html .css"
    " .scss .vue .svelte .ipynb".split()
)
_MANIFESTS = ("requirements*.txt", "pyproject.toml", "package.json", "setup.py", "setup.cfg", "pipfile", "go.mod", "cargo.toml")
_CONFIGS = ("dockerfile", "docker-compose*.yml", "makefile", "*.toml", "*.yaml", "*.yml", "*.ini", "*.cfg", ".env.example")
_LOCK = re.compile(r"(?:^|[-_.])lock\.")  # pnpm-lock.yaml, conda-lock.yml, requirements-lock.txt


def analyze_session(session: Session, settings: Settings) -> tuple[dict[str, Evidence], list[TestRun], list[Candidate]]:
    """Rule-engine analysis of one upload: evidence, test runs and up to 3 candidates."""
    evidence = build_evidence(session, settings.evidence_excerpt_chars)
    test_runs = find_test_runs(session.events)
    return evidence, test_runs, build_candidates(session.events, evidence, test_runs)


def build_candidates(events: list[Event], evidence: dict[str, Evidence], test_runs: list[TestRun]) -> list[Candidate]:
    """Request units with a code or config edit, ranked by score then earlier turn; at most 3."""
    units: dict[int, list[Event]] = {}
    for event in events:
        if event.turn >= 1:
            units.setdefault(event.turn, []).append(event)
    scored = []
    for turn, unit in units.items():
        edits = [e for e in unit if file_kind(edit_file(e) or "")]
        if edits:
            signals, changed = _signals(unit, edits)
            score = sum(WEIGHTS[code] for code in signals) + min(changed / 20, 2)
            scored.append((score, turn, unit, edits, signals))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        _candidate(rank, *item, events, evidence, test_runs) for rank, item in enumerate(scored[:MAX_CANDIDATES], 1)
    ]


def file_kind(path: str) -> str | None:
    """Kind of a file whose edit can be a decision ("manifest", "code", "config"); None for docs and lock files."""
    name = re.split(r"[\\/]", path)[-1].lower()
    is_code = os.path.splitext(name)[1] in _CODE_EXTENSIONS
    if not name or (not is_code and _LOCK.search(name)):
        return None
    if any(fnmatchcase(name, pattern) for pattern in _MANIFESTS):
        return "manifest"
    if is_code:
        return "code"
    if any(fnmatchcase(name, pattern) for pattern in _CONFIGS):
        return "config"
    return None


def _signals(unit: list[Event], edits: list[Event]) -> tuple[list[str], int]:
    """Signal codes in labels.SIGNALS order, and added+removed line count, from the unit up to its decision."""
    decision = edits[-1].idx
    found: set[str] = set()
    added: list[str] = []
    changed = 0
    for edit in edits:
        for old, new in edit_hunks(edit):
            old_lines, new_lines = old.splitlines(), new.splitlines()
            old_set, new_set = set(old_lines), set(new_lines)
            plus = [line for line in new_lines if line not in old_set]
            changed += len(plus) + sum(line not in new_set for line in old_lines)
            added += plus
        kind = file_kind(edit_file(edit) or "")
        if kind == "manifest":
            found.add("dependency_change")
        elif kind == "config":
            found.add("config_change")
    found.update(code for code, pattern in _ADDED_LINE_SIGNALS.items() if any(map(pattern.search, added)))
    if changed >= 30:
        found.add("large_change")
    before = [e for e in unit if e.idx < decision]
    if any(e.kind == "tool_use" and e.tool == "Bash" and _INSTALL.search((e.tool_input or {}).get("command", "")) for e in before):
        found.add("dependency_change")
    text = "\n".join(e.text for e in before if e.kind in ("user_text", "assistant_text"))
    found.update(code for code, pattern in _TEXT_SIGNALS.items() if pattern.search(text))
    return [code for code in labels.SIGNALS if code in found], changed


def _category(signals: list[str]) -> Category:
    if not signals:
        return "design_choice"
    sums = {group: sum(WEIGHTS[code] for code in codes if code in signals) for group, codes in _GROUPS.items()}
    return max(sums, key=sums.__getitem__)  # first maximum in _GROUPS order breaks ties


def _candidate(
    rank: int, score: float, turn: int, unit: list[Event], edits: list[Event], signals: list[str],
    events: list[Event], evidence: dict[str, Evidence], test_runs: list[TestRun],
) -> Candidate:
    decision = edits[-1].idx
    ids = [evidence_id(e.idx) for e in unit if e.idx <= decision]
    items = [evidence[i] for i in ids]
    has_request = len(items[0].excerpt) >= 10  # a unit always opens with its human request
    has_explanation = sum(len(item.excerpt) for item in items if item.kind == "assistant_text") >= 40
    files = list(dict.fromkeys(edit_file(e) for e in edits))
    candidate = Candidate(
        id=f"c{rank}",
        rank=rank,
        turn=turn,
        request_idx=unit[0].idx,
        edit_idxs=[e.idx for e in edits],
        decision_idx=decision,
        files=files,
        category=_category(signals),
        signals=signals,
        score=score,
        evidence_ids=ids,
        evidence_level=_LEVELS[has_request + has_explanation],
        has_test_record=any(run.use_idx > decision for run in test_runs),
        retro=retro_info(decision, events, test_runs),
    )
    end = analysis_window(candidate, events)[1]
    candidate.later_fix = any(edit_file(e) in files for e in events[end + 1 :])
    return candidate
