"""
Step 4 - narrow LLM-as-judge for infeasibility explanations (see
PHASE_6_BUILD_INSTRUCTIONS.md, Step 4).

Uses Gemini rather than the build doc's original Claude suggestion -
GEMINI_API_KEY is the only LLM credential actually available for this
project right now (it already does the pdf_parser.py vision-fallback
job; see router.py). Deviation from Schedule_Optimizer_Project_Notes.md
Section 7's "second Claude API call" note - flagged there for the notes
doc to catch up, not silently substituted.

Scope, deliberately narrow: judges ONE thing - a boss-facing explanation
of why a schedule is infeasible - not the schedule itself. Judging
schedule quality with an LLM was explicitly ruled out earlier in the
project (Schedule_Optimizer_Project_Notes.md, Section 5); Step 1's
deterministic suite owns correctness, permanently.

Split into two independently-checkable questions:
  (a) factual accuracy - does every specific claim the explanation makes
      (this person, this slot, this reason) match Step 2's diagnosis
      data? The model's ONLY job here is extraction: pull out what the
      explanation claims (person, slot, one of three canonical reason
      codes). Whether each claim is true is decided by plain Python
      cross-referencing those claims against SlotDiagnosis - never by
      the model's own say-so - so "accurate" can't be a rubber stamp.
  (b) clarity - would a non-technical reader (the boss) understand why
      the schedule failed without needing to know what CP-SAT or a
      constraint solver is? This one is a genuine judgment call, so it's
      the model's call, not cross-referenced.
"""

import json
import os
from dataclasses import dataclass
from typing import Callable

from diagnose import SlotDiagnosis

JUDGE_MODEL = "gemini-2.5-flash"

VALID_REASONS = ("not_available", "not_tech_capable", "at_hours_cap")

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "description": "One entry per person the explanation names as ruled out for a specific slot.",
            "items": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string"},
                    "day": {"type": "string", "description": "e.g. 'Sat', 'Sun'"},
                    "time_label": {
                        "type": "string",
                        "description": "the clock time shown in the explanation, e.g. '12:00'",
                    },
                    "reason": {
                        "type": "string",
                        "enum": list(VALID_REASONS),
                        "description": (
                            "not_available = explanation says they weren't available that slot; "
                            "not_tech_capable = explanation says they're assistant-only/can't do tech; "
                            "at_hours_cap = explanation says they're already at their hour limit."
                        ),
                    },
                },
                "required": ["person_name", "day", "time_label", "reason"],
            },
        },
        "clear": {
            "type": "boolean",
            "description": (
                "Would a non-technical reader (a manager with no CP-SAT/constraint-solver "
                "background) understand why the schedule failed, from this text alone?"
            ),
        },
        "clarity_notes": {
            "type": "string",
            "description": "One or two sentences on why (especially if not clear).",
        },
    },
    "required": ["claims", "clear", "clarity_notes"],
}

_PROMPT_TEMPLATE = """\
Here is an explanation shown to a non-technical manager for why a work \
schedule could not be built:

{explanation_text}

Extract every (person, slot, reason) claim it makes, and judge whether a \
non-technical reader would understand it.
"""


@dataclass
class ClaimMismatch:
    """One claim the explanation made that doesn't match Step 2's ground truth."""

    person_name: str
    day: str
    time_label: str
    claimed_reason: str
    actual_reason: str | None  # None if there's no ruling for this person/slot at all


@dataclass
class JudgeResult:
    accurate: bool
    mismatches: list[ClaimMismatch]
    clear: bool
    clarity_notes: str
    raw_claims: list[dict]


def _extract_claims_and_clarity(explanation_text: str) -> dict:
    """The only LLM call in this module. Extraction + a clarity opinion -
    no accuracy verdict is ever asked of the model; see module docstring.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "judge.py needs GEMINI_API_KEY set to call the judge model. Add it to .env (see .env.example)."
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=JUDGE_MODEL,
        contents=[_PROMPT_TEMPLATE.format(explanation_text=explanation_text)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=_RESPONSE_SCHEMA,
        ),
    )
    return json.loads(response.text)


def judge_explanation(
    diagnoses: list[SlotDiagnosis],
    explanation_text: str,
    extractor: Callable[[str], dict] = _extract_claims_and_clarity,
) -> JudgeResult:
    """Judge `explanation_text` (boss-facing text, e.g. explain_node's
    output) against `diagnoses` (Step 2's ground truth for the same solve).

    `extractor` defaults to the real Gemini API call; tests inject a fake
    to exercise the cross-referencing logic below without a live API key.
    """
    extracted = extractor(explanation_text)

    truth_by_key: dict[tuple[str, str, str], str] = {}
    for diag in diagnoses:
        for ruling in diag.rulings:
            truth_by_key[(diag.day, diag.time_label, ruling.person_name)] = ruling.reason

    mismatches = []
    for claim in extracted["claims"]:
        key = (claim["day"], claim["time_label"], claim["person_name"])
        actual_reason = truth_by_key.get(key)
        if actual_reason != claim["reason"]:
            mismatches.append(
                ClaimMismatch(
                    person_name=claim["person_name"],
                    day=claim["day"],
                    time_label=claim["time_label"],
                    claimed_reason=claim["reason"],
                    actual_reason=actual_reason,
                )
            )

    return JudgeResult(
        accurate=len(mismatches) == 0,
        mismatches=mismatches,
        clear=extracted["clear"],
        clarity_notes=extracted["clarity_notes"],
        raw_claims=extracted["claims"],
    )
