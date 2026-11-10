"""Comparison of model outputs by cited evidence ids only (ARCHITECTURE §5.5). Agreement is never truth."""

from __future__ import annotations

from typing import Any

from agency_loop import labels

NOTE = "의견이 같아도 사실로 확정하지 않습니다"


def compare(by_provider: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Groups graded statements or points by valid cited id (ids not in `invalid_ids`); meaning is never judged.

    same_evidence / only_one: `[{evidence_id, providers, statements: {provider: [...]}}]` in first-cited order.
    different: `[{provider, statements}]`, the statements with no valid id (not comparable).
    """
    cited: dict[str, dict[str, list[dict[str, Any]]]] = {}
    different = []
    for provider, statements in by_provider.items():
        loose = []
        for statement in statements:
            invalid = set(statement.get("invalid_ids") or [])
            ids = [i for i in dict.fromkeys(statement.get("evidence_ids") or []) if i not in invalid]
            for evidence_id in ids:
                cited.setdefault(evidence_id, {}).setdefault(provider, []).append(statement)
            if not ids:
                loose.append(statement)
        if loose:
            different.append({"provider": provider, "statements": loose})
    groups: dict[str, list[dict[str, Any]]] = {"same_evidence": [], "only_one": []}
    for evidence_id, by in cited.items():
        group = "same_evidence" if len(by) >= 2 else "only_one"
        groups[group].append({"evidence_id": evidence_id, "providers": list(by), "statements": by})
    return {**groups, "different": different, "labels": dict(labels.COMPARISON), "note": NOTE}
