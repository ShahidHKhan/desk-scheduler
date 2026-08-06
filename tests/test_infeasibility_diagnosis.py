"""
Step 2 - infeasibility-diagnosis layer. When a weekend slot has zero
valid tech candidates, the diagnosis must name the failing slot(s) and
attribute each ruled-out candidate's exclusion to the actual reason
(availability, capability, or hours-cap) - not a generic "infeasible"
message, and not the wrong reason.
"""

from diagnose import diagnose_coverage_gaps, diagnose_coverage_gaps_detailed
from helpers import make_person, only_available
from model_input import SolverInput
from solve import solve


def test_no_weekend_available_tech_capable_person_names_slot_and_real_reasons():
    # Tech1 is tech-capable but only available on weekdays - never weekends.
    # Asst1 is available all week (including weekends) but is assistant-only.
    # So every weekend slot has zero qualifying candidates, for two
    # different reasons depending on who you look at.
    tech = make_person(1, "Tech1", "tech_only", availability=only_available("Mon", range(0, 24)))
    asst = make_person(2, "Asst1", "assistant_only", availability=only_available("Sat", range(8, 18)))
    data = SolverInput(people=[tech, asst])

    result = solve(data, time_limit_seconds=10)
    assert not result["feasible"]
    assert result["status"] == "INFEASIBLE"
    assert result["coverage_gaps"], "expected coverage_gaps to explain the failure, not just INFEASIBLE"

    diagnoses = diagnose_coverage_gaps_detailed(data)
    assert diagnoses, "expected at least one unsatisfiable weekend slot"

    sat_slot = next(d for d in diagnoses if d.day == "Sat" and d.slot == 8)
    reasons_by_person = {r.person_name: r.reason for r in sat_slot.rulings}
    assert reasons_by_person["Tech1"] == "not_available"
    assert reasons_by_person["Asst1"] == "not_tech_capable"

    # A weekend slot Asst1 isn't even available for (Sun) should attribute
    # Asst1's exclusion to availability, not capability - proves the
    # ordering/logic is per-slot, not a single roster-wide verdict per person.
    sun_slot = next(d for d in diagnoses if d.day == "Sun" and d.slot == 8)
    sun_reasons = {r.person_name: r.reason for r in sun_slot.rulings}
    assert sun_reasons["Asst1"] == "not_available"
    assert sun_reasons["Tech1"] == "not_available"

    messages = diagnose_coverage_gaps(data)
    assert any("Sat 12:00" in m for m in messages)
    assert any("not available this slot" in m or "assistant-only" in m for m in messages)


def test_hours_cap_conflict_is_attributed_to_hours_cap_not_availability():
    # Only tech-capable person on the roster is available for every weekend
    # slot and would otherwise be a perfectly valid candidate - except they
    # requested 0 hours, so they have no capacity to give. The diagnosis
    # must say so specifically, not claim they're unavailable or incapable.
    tech = make_person(
        1, "Tech1", "tech_only", hours_requested=0, availability=only_available("Sat", range(8, 18))
    )
    asst = make_person(
        2, "Asst1", "assistant_only", availability=only_available("Sat", range(8, 18))
    )
    data = SolverInput(people=[tech, asst])

    result = solve(data, time_limit_seconds=10)
    assert not result["feasible"]

    diagnoses = diagnose_coverage_gaps_detailed(data)
    assert diagnoses

    sat_slot = next(d for d in diagnoses if d.day == "Sat" and d.slot == 8)
    reasons_by_person = {r.person_name: r.reason for r in sat_slot.rulings}
    assert reasons_by_person["Tech1"] == "at_hours_cap"
    assert reasons_by_person["Tech1"] != "not_available"
    assert reasons_by_person["Asst1"] == "not_tech_capable"

    tech_ruling = next(r for r in sat_slot.rulings if r.person_name == "Tech1")
    assert "hour cap" in tech_ruling.detail

    messages = diagnose_coverage_gaps(data)
    assert any("hour cap" in m for m in messages)


def test_diagnose_returns_nothing_when_weekend_coverage_is_satisfiable():
    tech = make_person(1, "Tech1", "tech_only")  # full_week_availability(), hours_requested=15
    data = SolverInput(people=[tech])
    assert diagnose_coverage_gaps_detailed(data) == []
    assert diagnose_coverage_gaps(data) == []
