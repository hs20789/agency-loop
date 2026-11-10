"""Development-process report (docs/ARCHITECTURE.md §5.10): section grading and the gap summary reuse.

The report (Ⓔ) runs on the analysis bundle after the retro prediction gate, under the analysis consent. Its
statements carry evidence ids like Ⓐ's sections, and app code — not the model — decides whether a quote is a
verified record. The stored gap points (C section) are reused as prompt context by the What-if (Ⓑ) and both
feedback calls (Ⓒ·Ⓓ); everything else in the report never reaches another model call.
"""

from __future__ import annotations

from typing import Any

from agency_loop import labels
from agency_loop.flow.bundle import BundleItem
from agency_loop.flow.citations import grade_statements
from agency_loop.flow.schemas import REPORT_SUMMARY_SECTIONS
from agency_loop.model import Workspace

GAP_PROMPT_LIMIT = 8  # most this many gap points go to the What-if and feedback prompts


def grade_report(data: dict[str, Any], items: list[BundleItem]) -> tuple[dict[str, Any], dict[str, int], int]:
    """Graded report sections and citation stats over every report statement (summary groups, finding
    observations and gaps), plus the count of action ids that were not in the sent bundle (actions only
    reference evidence, so their ids are filtered, not graded)."""
    sections: dict[str, Any] = {}
    stats: dict[str, int] = {}
    for name in REPORT_SUMMARY_SECTIONS:
        sections[name], counts = grade_statements(data[name], items)
        stats = _add(stats, counts)
    findings: list[dict[str, Any]] = []
    for finding in data["prompt_findings"]:
        graded, counts = grade_statements([finding["observation"]], items)
        stats = _add(stats, counts)
        findings.append({
            "aspect": finding["aspect"],
            "aspect_label": labels.PROMPT_ASPECT[finding["aspect"]],
            "observation": graded[0],
            "suggestion": finding["suggestion"],
        })
    sections["prompt_findings"] = findings
    gaps: list[dict[str, Any]] = []
    for gap in data["gaps"]:
        statement = {key: gap[key] for key in ("text", "kind", "evidence_ids", "quote")}
        graded, counts = grade_statements([statement], items)
        stats = _add(stats, counts)
        gaps.append({"concern": gap["concern"], "concern_label": labels.GAP_CONCERN[gap["concern"]], **graded[0]})
    sections["gaps"] = gaps
    sent = {item.evidence_id for item in items}
    dropped = 0
    actions: list[dict[str, Any]] = []
    for action in data["actions"]:
        dropped += len([i for i in action["evidence_ids"] if i not in sent])
        actions.append({
            "kind": action["kind"],
            "kind_label": labels.ACTION_KIND[action["kind"]],
            "text": action["text"],
            "evidence_ids": [i for i in action["evidence_ids"] if i in sent],
        })
    sections["actions"] = actions
    return sections, stats, dropped


def review_gaps(ws: Workspace, sent: set[str], limit: int = GAP_PROMPT_LIMIT) -> list[dict[str, Any]]:
    """The stored report's gap points, compacted for a prompt: only valid statement keys and ids that were in the
    sent bundle. [] when no report was stored (the call then proceeds without them)."""
    report = ws.results.get("report") or {}
    gaps: list[dict[str, Any]] = []
    for gap in report.get("gaps", [])[:limit]:
        gaps.append({
            "concern": gap["concern"],
            "concern_label": gap["concern_label"],
            "text": gap["text"],
            "kind": gap["kind"],
            "evidence_ids": [i for i in gap["evidence_ids"] if i in sent],
        })
    return gaps


def _add(stats: dict[str, int], counts: dict[str, int]) -> dict[str, int]:
    return {key: stats.get(key, 0) + n for key, n in counts.items()}
