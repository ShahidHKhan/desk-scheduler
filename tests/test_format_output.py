"""
Display helpers: the schedule grid and hours table read only the solver
result, and slots are named by clock time.
"""

from scheduler.pipeline.format_output import build_hours_table, build_schedule_grid
from scheduler.solver.model_input import operating_slot_options, slot_label, time_label

RESULT = {
    "assignments": [
        {"person_id": 1, "name": "Jane Doe", "initials": "JD", "day": "Mon", "slot": 0, "role": "tech"},
        {"person_id": 2, "name": "Jane Doe", "initials": "JX", "day": "Mon", "slot": 0, "role": "assistant"},
        {"person_id": 3, "name": "No Initials", "initials": "", "day": "Sat", "slot": 8, "role": "tech"},
    ],
    "people": [
        {"person_id": 1, "name": "Jane Doe", "initials": "JD", "hours_requested": 10, "hours_assigned": 0.5},
        {"person_id": 2, "name": "Jane Doe", "initials": "JX", "hours_requested": 8, "hours_assigned": 0.5},
        {"person_id": 3, "name": "No Initials", "initials": "", "hours_requested": 5, "hours_assigned": 0.5},
    ],
}


def test_grid_uses_initials_and_falls_back_to_name():
    grid = build_schedule_grid(RESULT)
    assert grid.loc["08:00", "Mon"] == "JD, JX"
    assert grid.loc["12:00", "Sat"] == "No Initials"
    assert grid.loc["08:00", "Sat"] == "Closed"


def test_hours_table_keeps_people_who_share_a_name():
    table = build_hours_table(RESULT)
    assert len(table) == 3
    assert list(table.loc[table["Name"] == "Jane Doe", "Hours requested"]) == [10, 8]


def test_slot_labels():
    assert time_label(5) == "10:30"
    assert slot_label("Mon", 5) == "Mon 10:30"
    assert slot_label("Mon", 99) == "Mon slot 99"
    assert operating_slot_options("Sat")[0] == (8, "12:00-12:30")
    assert operating_slot_options("Fri")[-1] == (17, "16:30-17:00")
