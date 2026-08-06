"""
Step 4 - LLM-as-judge for infeasibility explanations.

The cross-referencing logic (claims -> mismatches -> accurate/not) is
fully deterministic and is what actually decides the accuracy verdict
(see judge.py's module docstring) - it's tested here with an injected
fake `extractor`, shaped like a real Claude tool-call response, so the
logic that matters is verified without needing network access or an API
key. test_judge_live_api_call_end_to_end below additionally exercises
the real model call when a key is available.
"""

import os

import pytest

from diagnose import diagnose_coverage_gaps_detailed
from helpers import make_person, only_available
from judge import judge_explanation
from model_input import SolverInput


def _availability_and_capability_scenario():
    # Same shape as tests/test_infeasibility_diagnosis.py's availability-cause
    # scenario: Tech1 is tech-capable but never available weekends; Asst1 is
    # available Saturday but assistant-only.
    tech = make_person(1, "Tech1", "tech_only", availability=only_available("Mon", range(0, 24)))
    asst = make_person(2, "Asst1", "assistant_only", availability=only_available("Sat", range(8, 18)))
    data = SolverInput(people=[tech, asst])
    return diagnose_coverage_gaps_detailed(data)


def _hours_cap_scenario():
    # Same shape as test_infeasibility_diagnosis.py's hours-cap-cause
    # scenario: Tech1 would qualify (tech-capable, available) except for
    # requesting 0 hours.
    tech = make_person(
        1, "Tech1", "tech_only", hours_requested=0, availability=only_available("Sat", range(8, 18))
    )
    asst = make_person(2, "Asst1", "assistant_only", availability=only_available("Sat", range(8, 18)))
    data = SolverInput(people=[tech, asst])
    return diagnose_coverage_gaps_detailed(data)


def _perfect_extractor_for(diagnoses):
    """Simulates a real LLM correctly extracting every claim from an
    explanation that was generated straight from `diagnoses` (see
    SlotDiagnosis.to_message()) - every extracted claim matches ground
    truth exactly, since there's nothing to get wrong yet."""

    def extractor(explanation_text: str) -> dict:
        claims = [
            {
                "person_name": ruling.person_name,
                "day": diag.day,
                "time_label": diag.time_label,
                "reason": ruling.reason,
            }
            for diag in diagnoses
            for ruling in diag.rulings
        ]
        return {
            "claims": claims,
            "clear": True,
            "clarity_notes": "Names each person and the specific reason they were ruled out.",
        }

    return extractor


def test_judge_marks_accurate_explanation_as_accurate_availability_cause():
    diagnoses = _availability_and_capability_scenario()
    explanation_text = "\n".join(d.to_message() for d in diagnoses)

    result = judge_explanation(diagnoses, explanation_text, extractor=_perfect_extractor_for(diagnoses))

    assert result.accurate, f"expected no mismatches, got: {result.mismatches}"
    assert result.mismatches == []


def test_judge_marks_accurate_explanation_as_accurate_hours_cap_cause():
    diagnoses = _hours_cap_scenario()
    explanation_text = "\n".join(d.to_message() for d in diagnoses)

    result = judge_explanation(diagnoses, explanation_text, extractor=_perfect_extractor_for(diagnoses))

    assert result.accurate, f"expected no mismatches, got: {result.mismatches}"


def test_judge_flags_corrupted_explanation_as_inaccurate():
    diagnoses = _hours_cap_scenario()
    explanation_text = "\n".join(d.to_message() for d in diagnoses)
    correct_extractor = _perfect_extractor_for(diagnoses)

    # Simulate a corrupted/wrong explanation: the extractor reports Tech1's
    # exclusion reason as "not_available" when the real ground truth (Step
    # 2's diagnosis) says "at_hours_cap" - the judge must catch this, not
    # rubber-stamp whatever it's given.
    def corrupted_extractor(explanation_text: str) -> dict:
        extracted = correct_extractor(explanation_text)
        for claim in extracted["claims"]:
            if claim["person_name"] == "Tech1":
                claim["reason"] = "not_available"
        return extracted

    result = judge_explanation(diagnoses, explanation_text, extractor=corrupted_extractor)

    assert not result.accurate
    assert any(
        m.person_name == "Tech1" and m.claimed_reason == "not_available" and m.actual_reason == "at_hours_cap"
        for m in result.mismatches
    )


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason=(
        "live Gemini API judge run needs GEMINI_API_KEY in the environment - "
        "the deterministic cross-reference logic that decides accuracy is already "
        "covered above without one"
    ),
)
def test_judge_live_api_call_end_to_end():
    """One real Gemini API call per Step 2 scenario - actually exercises
    the model's own claim extraction and clarity judgment (the fake
    extractor above only tests the cross-reference logic downstream of
    it). Only runs when a real key is configured.
    """
    for diagnoses in (_availability_and_capability_scenario(), _hours_cap_scenario()):
        explanation_text = "\n".join(d.to_message() for d in diagnoses)
        result = judge_explanation(diagnoses, explanation_text)
        assert result.accurate, f"mismatches: {result.mismatches}"
        assert result.clear, f"judged unclear: {result.clarity_notes}"
