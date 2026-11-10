"""Self-assessment: validation, What-if tier, self-rating vs answer comparison, next difficulty (ARCHITECTURE §5.6).

The raw self-assessment stays on the server; models only ever get the tier code. Nothing here is a score.
"""

from __future__ import annotations

from typing import Any

from agency_loop import labels

AXES = ("understanding", "verification", "control")
_OBSERVED = {"shown": 2, "partial": 1, "not_shown": 0}
_RELATION_TEXT = {
    "higher": "자기평가보다 이번 답변에 덜 드러났습니다. 알고 있는 내용을 답변에 구체적으로 적어 보세요.",
    "similar": "자기평가와 이번 답변에 드러난 정도가 비슷합니다.",
    "lower": "자기평가보다 이번 답변에 더 많이 드러났습니다.",
}
_NEXT_TEXT = {
    "harder": "다음에는 한 단계 어려운 상황으로 연습해 볼 수 있습니다.",
    "easier": "다음에는 한 단계 쉬운 상황으로 연습해 볼 수 있습니다.",
    "same": "다음에도 같은 난이도로 연습해 볼 수 있습니다.",
}


def validate(value: Any) -> dict[str, int]:
    """Exactly the three axes, each an int 0..2 (bools rejected). Raises ValueError otherwise."""
    if not isinstance(value, dict) or set(value) != set(AXES):
        raise ValueError("self_assessment")
    if any(type(value[axis]) is not int or not 0 <= value[axis] <= 2 for axis in AXES):
        raise ValueError("self_assessment")
    return {axis: value[axis] for axis in AXES}


def tier_for(sa: dict[str, int]) -> str:
    total = sum(sa[axis] for axis in AXES)
    return "basic" if total <= 2 else "standard" if total <= 4 else "advanced"


def compare_axes(sa: dict[str, int], axes: dict[str, str]) -> list[dict[str, Any]]:
    """Per axis: the self-rating relative to what the answer showed ("higher" = rated above what was shown)."""
    rows = []
    for axis in AXES:
        rating, shown = sa[axis], axes[axis]
        observed = _OBSERVED[shown]
        relation = "higher" if rating > observed else "lower" if rating < observed else "similar"
        rows.append({
            "axis": axis,
            "axis_label": labels.AXIS[axis],
            "self_rating": rating,
            "self_label": labels.SELF_RATING[rating],
            "observed": shown,
            "observed_label": labels.AXIS_EVIDENCE[shown],
            "relation": relation,
            "message": f"{labels.AXIS[axis]}: 자기평가는 '{labels.SELF_RATING[rating]}', 이번 답변에서는 "
                       f"'{labels.AXIS_EVIDENCE[shown]}'입니다. {_RELATION_TEXT[relation]}",
        })
    return rows


def next_difficulty(tier: str, axes: dict[str, str]) -> dict[str, str]:
    """A text suggestion only; the next task is never generated."""
    shown = [axes[axis] for axis in AXES]
    if all(value == "shown" for value in shown):
        code = "same" if tier == "advanced" else "harder"
    elif "not_shown" in shown:
        code = "same" if tier == "basic" else "easier"
    else:
        code = "same"
    return {"code": code, "label": labels.NEXT_DIFFICULTY[code], "message": _NEXT_TEXT[code]}
