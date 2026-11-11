"""JSON schemas for structured model output (docs/ARCHITECTURE.md §5.3).

Structured-output rules: every object sets additionalProperties false and lists all keys as required;
no min/max or length constraints.
"""

from __future__ import annotations

import jsonschema


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}
_STRS = {"type": "array", "items": {"type": "string"}}

STATEMENT = _obj(
    {
        "text": _STR,
        "kind": {"type": "string", "enum": ["record", "interpretation", "needs_verification"]},
        "evidence_ids": _IDS,
        "quote": _STR,
    }
)
_STATEMENTS = {"type": "array", "items": STATEMENT}

ANALYSIS = _obj(
    {
        "decision_summary": _STR,
        "reasons": _STATEMENTS,
        "alternatives": _STATEMENTS,
        "assumptions": _STATEMENTS,
        "risks": _STATEMENTS,
        "verification_points": _STATEMENTS,
        "scope_note": _STR,
    }
)
ANALYSIS_SECTIONS = ("reasons", "alternatives", "assumptions", "risks", "verification_points")

ANSWER_FIELDS = ("expected_behavior", "potential_issue", "ai_instruction", "verification")

# Development-process report (ARCHITECTURE §5.3): the four summary statement groups and the graded enum lists.
REPORT_SUMMARY_SECTIONS = ("tasks", "changes", "decisions", "tests")
PROMPT_ASPECTS = ("clarity", "context_constraints", "exceptions", "verification", "scope_expectation")
GAP_CONCERNS = (
    "missing_verification", "unclear_rationale", "requirement_test_gap", "nonfunctional_risk", "post_failure_continuation",
)
ACTION_KINDS = ("ask_ai", "requirements", "tests", "alternatives")

REPORT = _obj(
    {
        "overview": _STR,
        "tasks": _STATEMENTS,
        "changes": _STATEMENTS,
        "decisions": _STATEMENTS,
        "tests": _STATEMENTS,
        "not_determined": _STRS,
        "prompt_findings": {
            "type": "array",
            "items": _obj(
                {
                    "aspect": {"type": "string", "enum": list(PROMPT_ASPECTS)},
                    "observation": STATEMENT,
                    "suggestion": _STR,
                }
            ),
        },
        "gaps": {
            "type": "array",
            "items": _obj(
                {
                    "concern": {"type": "string", "enum": list(GAP_CONCERNS)},
                    "text": _STR,
                    "kind": STATEMENT["properties"]["kind"],
                    "evidence_ids": _IDS,
                    "quote": _STR,
                }
            ),
        },
        "actions": {
            "type": "array",
            "items": _obj(
                {
                    "kind": {"type": "string", "enum": list(ACTION_KINDS)},
                    "text": _STR,
                    "evidence_ids": _IDS,
                }
            ),
        },
    }
)

WHATIF = _obj(
    {
        "can_generate": {"type": "boolean"},
        "reason": _STR,
        "title": _STR,
        "scenario": _STR,
        "hypothetical_changes": _STRS,
        "related_evidence_ids": _IDS,
        "questions": _obj({name: _STR for name in ANSWER_FIELDS}),
    }
)

_AX = {"type": "string", "enum": ["shown", "partial", "not_shown"]}

FEEDBACK = _obj(
    {
        "items": {
            "type": "array",
            "items": _obj(
                {
                    "field": {"type": "string", "enum": list(ANSWER_FIELDS)},
                    "strengths": _STRS,
                    "gaps": _STRS,
                    "hint": _STR,
                    "evidence_ids": _IDS,
                }
            ),
        },
        "instruction_review": _obj(
            {"has_requirements": {"type": "boolean"}, "has_verification_steps": {"type": "boolean"}, "note": _STR}
        ),
        "axes": _obj({"understanding": _AX, "verification": _AX, "control": _AX}),
        "coaching": _STR,
        "explanation": _STR,
    }
)

REVIEW = _obj(
    {
        "summary": _STR,
        "points": {
            "type": "array",
            "items": _obj(
                {
                    "type": {
                        "type": "string",
                        "enum": ["judgment", "issue", "counterexample", "alternative", "missing_evidence"],
                    },
                    "text": _STR,
                    "evidence_ids": _IDS,
                    "quote": _STR,
                }
            ),
        },
    }
)

BY_STAGE = {
    "analysis": ANALYSIS, "report": REPORT, "whatif": WHATIF, "feedback1": FEEDBACK,
    "feedback2": FEEDBACK, "review": REVIEW,
}


# Keys a model without enforced structured output (HCX-007) tends to leave out when it has nothing to put there.
# Only these are filled in; any other missing key still fails the schema check.
_FILLABLE = {"quote": "", "evidence_ids": []}


def repair(data: object, schema: dict) -> object:
    """Fills the `_FILLABLE` keys a reply left out and drops keys the schema does not allow, recursively.
    Run before `is_valid` on output that no API enforced; it never invents content, so anything else wrong stays wrong."""
    if schema.get("type") == "object" and isinstance(data, dict):
        props = schema.get("properties", {})
        out = {k: repair(v, props[k]) for k, v in data.items() if k in props or schema.get("additionalProperties", True)}
        for key in schema.get("required", []):
            if key not in out and key in _FILLABLE:
                out[key] = type(_FILLABLE[key])(_FILLABLE[key])
        return out
    if schema.get("type") == "array" and isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [repair(item, schema["items"]) for item in data]
    return data


def is_valid(data: object, schema: dict) -> bool:
    """True when `data` matches `schema` (used for mock, HCX-007 and DeepSeek output and as a Claude double check)."""
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError:
        return False
    return True
