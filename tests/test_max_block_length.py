"""
Rule 7 (hard, 6 hrs): no one's assigned block ever exceeds 6
continuous hours (12 half-hour slots) in a single stretch.
"""

from helpers import make_person, only_available

from scheduler.solver.model_input import SolverInput
from scheduler.solver.solve import solve


def _longest_run(slots: list[int]) -> int:
    """Longest run of consecutive integers in `slots`."""
    if not slots:
        return 0
    ordered = sorted(slots)
    longest = current = 1
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        current = current + 1 if cur == prev + 1 else 1
        longest = max(longest, current)
    return longest


def test_no_single_block_exceeds_six_hours():
    # LongTech is available for all 24 operating slots on Monday (12 hrs)
    # and nowhere else - with hours_requested=20 (far more than Monday
    # alone could ever supply), the fairness objective has every incentive
    # to pack them into one long continuous Monday block if nothing stops
    # it. A second tech covers the rest of the week/weekend so the model
    # stays feasible regardless of how LongTech's day plays out.
    long_tech = make_person(
        id=1, name="LongTech", role_weighting="tech_only", hours_requested=20,
        availability=only_available("Mon", range(0, 24)),
    )
    other_tech = make_person(id=2, name="OtherTech", role_weighting="tech_only", hours_requested=20)
    asst1 = make_person(id=3, name="Asst1", role_weighting="assistant_only", hours_requested=20)
    asst2 = make_person(id=4, name="Asst2", role_weighting="assistant_only", hours_requested=20)

    data = SolverInput(people=[long_tech, other_tech, asst1, asst2])
    result = solve(data, time_limit_seconds=15)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    monday_slots = sorted(
        a["slot"] for a in result["assignments"] if a["person"] == "LongTech" and a["day"] == "Mon"
    )
    assert monday_slots, "expected the solver to actually use LongTech's Monday availability"

    longest_run = _longest_run(monday_slots)
    assert longest_run <= 12, f"LongTech worked a {longest_run}-slot ({longest_run * 0.5} hr) continuous block"

    # Strengthen the exercise: confirm the solver was actually pushing past
    # what a single legal block could hold (otherwise this scenario could
    # trivially pass just because the solver happened to stop early).
    assert len(monday_slots) > 12, (
        f"LongTech only worked {len(monday_slots)} Monday slots - scenario didn't actually "
        f"pressure the solver past a single 6 hr block"
    )
