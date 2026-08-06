"""
Tier 1 - Rule 2 (hard): an Assistant-only person is never assigned a tech
slot, even when tech coverage is short and they're available to "help".
"""

from helpers import make_person
from model_input import SolverInput
from solve import solve


def test_assistant_only_never_assigned_tech_role_even_when_tech_coverage_is_short():
    # Exactly one tech-capable person for the whole roster - weekday tech
    # coverage (target 2) will be short at literally every slot, and
    # Asst1 is available every one of those slots too. If the capability
    # boundary weren't a hard constraint, the objective's heavy
    # COVERAGE_SHORTFALL_WEIGHT gives the solver every reason to plug
    # Asst1 into the open tech slots.
    only_tech = make_person(id=1, name="OnlyTech", role_weighting="tech_only", hours_requested=20)
    asst1 = make_person(id=2, name="Asst1", role_weighting="assistant_only", hours_requested=20)

    data = SolverInput(people=[only_tech, asst1])
    result = solve(data, time_limit_seconds=15)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    tech_assignments_by_asst1 = [
        a for a in result["assignments"] if a["person"] == "Asst1" and a["role"] == "tech"
    ]
    assert not tech_assignments_by_asst1, (
        f"Asst1 (assistant_only) was assigned a tech slot: {tech_assignments_by_asst1}"
    )

    # Confirm the scenario really did leave tech coverage short (i.e. this
    # wasn't a no-op scenario where Asst1 was never actually tempting) -
    # only one tech-capable person exists, so the 2-tech weekday target
    # can never be hit.
    assert any("tech(s)" in m for m in result["coverage_shortfalls"]), (
        "expected weekday tech coverage to be short with only one tech-capable person on the roster"
    )
