"""Statement grading: app code, not the model, decides whether a quote is a verified record (ARCHITECTURE §5.4).

Valid ids are the ids of the items that were sent; quotes are checked against the escaped text that was sent,
with whitespace collapsed on both sides, and must lie within one stretch between the app's cut markers.
"""

from __future__ import annotations

import re
from typing import Any

from agency_loop import labels
from agency_loop.flow.bundle import CUT_MARKS, BundleItem

_MARKS = re.compile("|".join(map(re.escape, CUT_MARKS)))


def grade_statements(
    statements: list[dict[str, Any]], items: list[BundleItem]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """GradedStatements and stats `{total, recorded, inferred, needs_verification, citation_unverified}`."""
    texts = _texts(items)
    graded = []
    for statement in statements:
        ids, invalid = _ids(statement, texts)
        if invalid:
            grade = "citation_unverified"
        elif statement.get("kind") == "record":
            grade = "recorded" if _quoted(statement, ids, texts) else "citation_unverified"
        elif statement.get("kind") == "interpretation":
            grade = "inferred" if ids else "needs_verification"
        else:
            grade = "needs_verification"
        graded.append({
            "text": statement.get("text", ""),
            "kind": statement.get("kind"),
            "evidence_ids": ids,
            "quote": statement.get("quote", ""),
            "grade": grade,
            "grade_label": labels.GRADE[grade],
            "invalid_ids": invalid,
        })
    stats = {"total": len(graded), **{code: sum(s["grade"] == code for s in graded) for code in labels.GRADE}}
    return graded, stats


def grade_points(points: list[dict[str, Any]], items: list[BundleItem]) -> list[dict[str, Any]]:
    """Review points have no kind: a verified quote -> recorded, a quote not found -> citation_unverified,
    no quote -> inferred with a valid id, else needs_verification. Unknown ids -> citation_unverified."""
    texts = _texts(items)
    graded = []
    for point in points:
        ids, invalid = _ids(point, texts)
        if invalid:
            grade = "citation_unverified"
        elif _norm(point.get("quote") or ""):
            grade = "recorded" if _quoted(point, ids, texts) else "citation_unverified"
        else:
            grade = "inferred" if ids else "needs_verification"
        graded.append({
            "type": point.get("type"),
            "type_label": labels.POINT_TYPE.get(point.get("type"), ""),
            "text": point.get("text", ""),
            "evidence_ids": ids,
            "quote": point.get("quote", ""),
            "grade": grade,
            "grade_label": labels.GRADE[grade],
            "invalid_ids": invalid,
        })
    return graded


def _norm(text: str) -> str:
    return " ".join(text.split())


def _texts(items: list[BundleItem]) -> dict[str, list[str]]:
    """Each sent text as its uncut stretches: a quote across a cut joins text that is not adjacent in the session."""
    return {item.evidence_id: [_norm(part) for part in _MARKS.split(item.text)] for item in items}


def _ids(entry: dict[str, Any], texts: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    ids = list(entry.get("evidence_ids") or [])
    return ids, [i for i in ids if i not in texts]


def _quoted(entry: dict[str, Any], ids: list[str], texts: dict[str, list[str]]) -> bool:
    """The quote, whitespace collapsed, appears in one uncut stretch of one of the entry's own cited items."""
    quote = _norm(entry.get("quote") or "")
    return bool(quote) and any(quote in part for i in ids for part in texts[i])
