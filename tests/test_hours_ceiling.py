"""
Rule 1 (hard): nobody's total assigned hours exceed hours_requested.
"""

from helpers import make_person

from scheduler.solver.model_input import SolverInput
from scheduler.solver.solve import solve


def test_assigned_hours_never_exceed_requested_hours():
    # LowHoursTech is available every operating slot all week (would allow
    # far more than 4 hrs if the ceiling weren't enforced) but only
    # requested 4 - nothing about coverage or fairness would naturally cap
    # them there, since the objective doesn't penalize giving someone more
    # than they asked for; only the hard ceiling constraint does.
    low_hours = make_person(id=1, name="LowHoursTech", role_weighting="tech_only", hours_requested=4)
    # Plenty of slack elsewhere so the solver has no coverage-driven reason
    # to avoid over-assigning LowHoursTech - if anything it's tempting to,
    # since they're "free" fully-available capacity.
    other_tech = make_person(id=2, name="OtherTech", role_weighting="tech_only", hours_requested=20)
    asst1 = make_person(id=3, name="Asst1", role_weighting="assistant_only", hours_requested=20)
    asst2 = make_person(id=4, name="Asst2", role_weighting="assistant_only", hours_requested=20)

    data = SolverInput(people=[low_hours, other_tech, asst1, asst2])
    result = solve(data, time_limit_seconds=15)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    assigned = result["hours_assigned"].get("LowHoursTech", 0)
    assert assigned <= 4, f"LowHoursTech assigned {assigned} hrs, exceeding their 4 hr request"
