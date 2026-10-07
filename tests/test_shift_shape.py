"""
Shift shape (solve.py's second pass, build_model.py's shape penalty):
people work one role per shift and one shift per day unless coverage
needs otherwise - and when it does, the switch still happens.
"""

from collections import defaultdict

from helpers import make_person, only_available

from scheduler.solver.model_input import SolverInput
from scheduler.solver.solve import solve


def shifts_by_person_day(result: dict) -> dict[tuple[int, str], dict[int, str]]:
    """(person_id, day) -> {slot: role}"""
    worked = defaultdict(dict)
    for a in result["assignments"]:
        worked[a["person_id"], a["day"]][a["slot"]] = a["role"]
    return worked


def count_switches_and_extra_shifts(result: dict) -> tuple[int, int]:
    switches = extra_shifts = 0
    for slots in shifts_by_person_day(result).values():
        switches += sum(1 for s in slots if s - 1 in slots and slots[s - 1] != slots[s])
        extra_shifts += sum(1 for s in slots if s - 1 not in slots) - 1
    return switches, extra_shifts


def test_hybrids_keep_one_role_and_one_shift_per_day():
    # Everyone hybrid and free all week: nothing forces a switch or a split
    # day, so neither should happen.
    people = [make_person(i, f"Hybrid{i}", "hybrid_2nd", hours_requested=15) for i in range(1, 17)]
    result = solve(SolverInput(people=people), time_limit_seconds=20)

    assert result["feasible"]
    assert count_switches_and_extra_shifts(result) == (0, 0)


def test_role_switch_still_happens_when_it_covers_a_slot():
    # Monday, 10:00-14:00. Hybrid's shift opens beside two assistants, so
    # it has to start as tech; the techs' shift ends beside two assistants,
    # so they have to finish as tech. 12:00-14:00 has Hybrid and both techs,
    # three tech-capable people for two tech boxes: one of them has to
    # switch, or someone sits out and the desk loses coverage.
    def monday(slots: range) -> dict:
        return only_available("Mon", slots)

    weekend = only_available("Sat", range(8, 18))
    weekend["Sun"] = weekend["Sat"]
    people = [
        make_person(1, "Asst1", "assistant_only", hours_requested=2, availability=monday(range(4, 8))),
        make_person(2, "Asst2", "assistant_only", hours_requested=2, availability=monday(range(4, 8))),
        make_person(3, "Hybrid", "hybrid_2nd", hours_requested=4, availability=monday(range(4, 12))),
        make_person(4, "Tech1", "tech_only", hours_requested=4, availability=monday(range(8, 16))),
        make_person(5, "Tech2", "tech_only", hours_requested=4, availability=monday(range(8, 16))),
        make_person(6, "Asst3", "assistant_only", hours_requested=2, availability=monday(range(12, 16))),
        make_person(7, "Asst4", "assistant_only", hours_requested=2, availability=monday(range(12, 16))),
        make_person(8, "Weekend", "tech_only", hours_requested=10, availability=weekend),
    ]
    result = solve(SolverInput(people=people), time_limit_seconds=10)

    assert result["feasible"]
    assert all(p["hours_assigned"] == p["hours_requested"] for p in result["people"]), "nobody should sit out"
    assert count_switches_and_extra_shifts(result) == (1, 0)
