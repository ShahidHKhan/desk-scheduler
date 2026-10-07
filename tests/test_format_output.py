"""
Display helpers: the schedule grid, master sheet and hours table read only
the solver result, and slots are named by clock time.
"""

from scheduler.pipeline.format_output import build_hours_table, build_master_sheet, build_schedule_grid
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


def shift(person_id: int, initials: str, day: str, slots: range, role: str = "tech") -> list[dict]:
    return [
        {"person_id": person_id, "name": initials, "initials": initials, "day": day, "slot": s, "role": role}
        for s in slots
    ]


def boxes(sheet: dict, day: str, slot: int) -> list[str]:
    return [c["initials"] for c in sheet["rows"][slot]["cells"] if c["kind"] == "box" and c["day"] == day]


def test_master_sheet_keeps_each_shift_in_one_column():
    # AA starts after BB but sorts first: BB still keeps the box it opened in.
    assignments = shift(2, "BB", "Mon", range(0, 4)) + shift(1, "AA", "Mon", range(2, 6))
    assignments += shift(3, "CC", "Mon", range(0, 2), role="assistant")
    sheet = build_master_sheet({"assignments": assignments, "people": RESULT["people"]})

    assert boxes(sheet, "Mon", 0) == ["CC", "", "BB", ""]
    assert boxes(sheet, "Mon", 2) == ["", "", "BB", "AA"]
    assert boxes(sheet, "Mon", 4) == ["", "", "", "AA"]
    assert boxes(sheet, "Sat", 8) == [""]


def test_master_sheet_layout():
    sheet = build_master_sheet(RESULT)
    assert [d["hours"] for d in sheet["days"]] == ["8:00-8:00"] * 4 + ["8:00-5:00", "12:00-5:00", "12:00-5:00"]
    assert [r["time"] for r in sheet["rows"]][7:11] == ["11:30", "Noon", "12:30", "1:00"]
    assert sheet["days"][3]["heading"] == "Thurs"

    kinds = [[c["kind"] for c in row["cells"]] for row in sheet["rows"]]
    assert kinds[0][-2:] == ["before", "before"]  # weekend mornings, one block each
    assert kinds[1][-1] == "box"  # Fri's last box: the weekend block covers this row
    assert kinds[18][-3:] == ["closed", "closed", "closed"]  # Fri, Sat, Sun close at 5:00
    assert kinds[24][:4] == ["closed"] * 4  # Mon-Thu close at 8:00
    assert [c.get("text") for c in sheet["rows"][21]["cells"] if c["kind"] == "key"] == ["Assistant Role"]

    # Every row fills the 22 columns after the time column; the weekend
    # blocks span rows 0-7 from row 0.
    for slot, row in enumerate(sheet["rows"]):
        width = sum(c.get("colspan", 1) for c in row["cells"])
        assert width == (20 if 0 < slot < 8 else 22)
    assert sheet["people"] == [[("Jane Doe", "JD"), ("Jane Doe", "JX")], [("No Initials", "")]]


def test_slot_labels():
    assert time_label(5) == "10:30"
    assert slot_label("Mon", 5) == "Mon 10:30"
    assert slot_label("Mon", 99) == "Mon slot 99"
    assert operating_slot_options("Sat")[0] == (8, "12:00-12:30")
    assert operating_slot_options("Fri")[-1] == (17, "16:30-17:00")
