"""
Weekend coverage (hard): every weekend slot has exactly one
person assigned, and that person is tech-capable. No exceptions.
"""

from helpers import generous_roster

from scheduler.solver.model_input import OPERATING_SLOTS, WEEKEND_DAYS, SolverInput
from scheduler.solver.solve import solve


def test_every_weekend_slot_has_exactly_one_tech_capable_person():
    people = generous_roster(tech_count=4, assistant_count=4, hours_requested=20)
    tech_capable_names = {p.name for p in people if p.can_work_tech()}

    data = SolverInput(people=people)
    result = solve(data, time_limit_seconds=20)
    assert result["feasible"], f"expected a feasible solve, got {result['status']}"

    tech_count_by_slot: dict[tuple[str, int], int] = {}
    assistant_count_by_slot: dict[tuple[str, int], int] = {}
    assigned_person_by_slot: dict[tuple[str, int], str] = {}

    for a in result["assignments"]:
        if a["day"] not in WEEKEND_DAYS:
            continue
        key = (a["day"], a["slot"])
        if a["role"] == "tech":
            tech_count_by_slot[key] = tech_count_by_slot.get(key, 0) + 1
            assigned_person_by_slot[key] = a["name"]
        else:
            assistant_count_by_slot[key] = assistant_count_by_slot.get(key, 0) + 1

    for day in WEEKEND_DAYS:
        for slot in OPERATING_SLOTS[day]:
            key = (day, slot)
            assert tech_count_by_slot.get(key, 0) == 1, (
                f"{day} slot {slot}: expected exactly 1 tech assigned, got {tech_count_by_slot.get(key, 0)}"
            )
            assert assistant_count_by_slot.get(key, 0) == 0, (
                f"{day} slot {slot}: expected 0 assistants (weekends are tech-only), "
                f"got {assistant_count_by_slot.get(key, 0)}"
            )
            assert assigned_person_by_slot[key] in tech_capable_names, (
                f"{day} slot {slot}: assigned person {assigned_person_by_slot[key]!r} is not tech-capable"
            )
