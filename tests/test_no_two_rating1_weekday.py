"""
Tier 1 - Rule 4 (hard): no weekday slot ever has two rating-1 people
working simultaneously.
"""

from helpers import make_person, only_available
from model_input import SolverInput
from solve import solve


def test_two_rating1_people_never_share_a_weekday_slot():
    # Both rating-1 people are available ONLY on the same narrow weekday
    # window, alongside one higher-rated tech who's also available then
    # (and everywhere else, to keep the rest of the week/weekend
    # feasible). With a 2-tech weekday coverage target and three
    # candidates to pick two from, pairing the two rating-1s together is
    # a live, tempting option for the solver - exactly what the hard
    # constraint has to rule out.
    rating1_a = make_person(
        id=1, name="Rating1A", role_weighting="tech_only", experience_rating=1,
        hours_requested=20, availability=only_available("Wed", range(5, 10)),
    )
    rating1_b = make_person(
        id=2, name="Rating1B", role_weighting="tech_only", experience_rating=1,
        hours_requested=20, availability=only_available("Wed", range(5, 10)),
    )
    rating3_tech = make_person(id=3, name="Rating3Tech", role_weighting="tech_only", experience_rating=3, hours_requested=20)
    asst1 = make_person(id=4, name="Asst1", role_weighting="assistant_only", hours_requested=20)
    asst2 = make_person(id=5, name="Asst2", role_weighting="assistant_only", hours_requested=20)

    data = SolverInput(people=[rating1_a, rating1_b, rating3_tech, asst1, asst2])
    result = solve(data, time_limit_seconds=15)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    by_slot = {}
    for a in result["assignments"]:
        if a["day"] != "Wed" or a["person"] not in ("Rating1A", "Rating1B"):
            continue
        by_slot.setdefault(a["slot"], set()).add(a["person"])

    doubled_up = {slot: people for slot, people in by_slot.items() if len(people) > 1}
    assert not doubled_up, f"both rating-1 people were scheduled together at Wed slot(s): {doubled_up}"
