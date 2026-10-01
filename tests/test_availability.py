"""
Rule 3 (hard): nobody is ever assigned to a slot they didn't mark
available.
"""

from helpers import generous_roster, make_person, only_available

from scheduler.solver.model_input import SolverInput
from scheduler.solver.solve import solve


def test_no_assignment_falls_outside_marked_availability():
    narrow = make_person(
        id=100,
        name="NarrowTech",
        role_weighting="tech_only",
        hours_requested=10,  # far more than the 2.5 hrs their 5 slots could ever cover
        availability=only_available("Mon", range(10, 15)),  # exactly slots 10-14
    )
    people = generous_roster() + [narrow]
    data = SolverInput(people=people)

    result = solve(data, time_limit_seconds=15)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    narrow_assignments = [a for a in result["assignments"] if a["person"] == "NarrowTech"]
    # Exercise the rule for real, not vacuously: NarrowTech has strong
    # incentive (fairness objective) to be used somewhere, and free
    # coverage-slack to be used in - confirm the solver actually did place
    # them, not just that it never violated a rule it never got a chance to.
    assert narrow_assignments, "expected the solver to actually use NarrowTech's available slots"

    for a in narrow_assignments:
        assert a["day"] == "Mon", f"NarrowTech assigned on {a['day']}, never marked available that day"
        assert a["slot"] in range(10, 15), (
            f"NarrowTech assigned Mon slot {a['slot']}, outside their marked 10-14 window"
        )
