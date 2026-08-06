"""
Tier 1 - Step 0 regression: re-uploading one person's corrected
availability file must not drop everyone else's availability from the
solve.

Root cause (see models.py/crud.py/graph.py): the Roster table originally
had no availability column at all - availability only ever lived in a
single pipeline run's ephemeral state (PipelineState.availability_submissions).
graph._build_solver_input() built every roster member's Person from that
state, defaulting anyone NOT part of the current run's submissions to
availability={} - which Person.is_available() treats as unavailable
everywhere. A run that only re-uploaded one corrected file therefore made
every OTHER roster member invisible to the solver, not just skipped for
updates - observed as weekend coverage vanishing for everyone else.

The fix persists availability on the roster row itself
(Roster.availability_json, written by crud.upsert_from_submission()) and
has _build_solver_input() fall back to that persisted value for anyone not
part of the current run.
"""

import crud
import database
from graph import _build_solver_input
from helpers import full_week_availability
from schema import AvailabilitySubmission
from solve import solve as run_solver


def test_reupload_of_one_person_does_not_drop_others_availability():
    database.init_db()
    availability = full_week_availability()

    # Three tech-capable people. Bob's hours_requested is deliberately tiny
    # (3 hrs = 6 slots) so he alone can never cover the weekend's 20 slots
    # (10 hrs) - Alice and/or Cara MUST actually be available and used for
    # the weekend hard constraint to be satisfiable at all. This makes the
    # regression deterministic: if Alice/Cara's availability silently
    # became {}, the second solve below goes INFEASIBLE, not just "less
    # fair".
    alice = crud.add_person("Alice", "AL", "tech_only", 2, 2, 15)
    bob = crud.add_person("Bob", "BB", "tech_only", 2, 2, 3)
    cara = crud.add_person("Cara", "CA", "tech_only", 2, 2, 15)

    # Initial bulk upload: all three submit at once, exactly like the
    # normal roster_confirm_node -> crud.upsert_from_submission flow.
    for person in (alice, bob, cara):
        crud.upsert_from_submission(person.id, person.name, person.hours_requested, availability)

    initial_state = {
        "availability_submissions": [
            AvailabilitySubmission(name="Alice", hours_requested=15, availability=availability),
            AvailabilitySubmission(name="Bob", hours_requested=3, availability=availability),
            AvailabilitySubmission(name="Cara", hours_requested=15, availability=availability),
        ],
        "submission_roster_ids": {0: alice.id, 1: bob.id, 2: cara.id},
    }
    data = _build_solver_input(initial_state)
    result = run_solver(data, time_limit_seconds=15)
    assert result["feasible"], f"expected the initial 3-person solve to be feasible, got {result['status']}"

    # --- Simulate re-uploading ONLY Bob's corrected file ---
    # A later pipeline run whose availability_submissions/submission_roster_ids
    # cover just Bob. Alice's and Cara's availability must come from their
    # PERSISTED roster rows now, not from this run's (empty, for them) state.
    crud.upsert_from_submission(bob.id, "Bob", 3, availability)
    reupload_state = {
        "availability_submissions": [
            AvailabilitySubmission(name="Bob", hours_requested=3, availability=availability),
        ],
        "submission_roster_ids": {0: bob.id},
    }

    data2 = _build_solver_input(reupload_state)

    # Sanity check on the fix itself, independent of the solve: Alice and
    # Cara's Person objects must carry their persisted availability, not {}.
    people_by_name = {p.name: p for p in data2.people}
    assert any(people_by_name["Alice"].availability.get(day) for day in people_by_name["Alice"].availability), (
        "Alice's availability was empty after a re-upload that didn't include her - "
        "the Step 0 roster-scoping bug"
    )
    assert any(people_by_name["Cara"].availability.get(day) for day in people_by_name["Cara"].availability), (
        "Cara's availability was empty after a re-upload that didn't include her - "
        "the Step 0 roster-scoping bug"
    )

    result2 = run_solver(data2, time_limit_seconds=15)
    assert result2["feasible"], (
        f"expected the re-solve after Bob's solo re-upload to still be feasible, got "
        f"{result2['status']} (coverage_gaps={result2.get('coverage_gaps')}) - Bob alone (3 hr "
        f"budget) cannot cover the weekend's 20 slots, so this failing means Alice/Cara's "
        f"availability was lost"
    )

    weekend_people_after = {a["person"] for a in result2["assignments"] if a["day"] in ("Sat", "Sun")}
    assert "Alice" in weekend_people_after or "Cara" in weekend_people_after, (
        f"expected Alice and/or Cara to still cover weekend slots after Bob's solo re-upload, "
        f"got weekend coverage from: {weekend_people_after}"
    )
