"""
Display tables for a solver result: the master-schedule grid (TIME_SLOTS x
DAYS, "Closed" outside operating hours - matching the real
Blank_Schedule.xlsx template's own convention), the same schedule laid out
like the desk's own master schedule sheet, and per-person hours.

All of them read only the result itself (see solver/solve.py), which
carries each person's name and initials, so they work the same for a
schedule saved months ago as for one just solved.
"""

import math
from collections import defaultdict

import pandas as pd

from scheduler.ingest.schema import TIME_SLOTS
from scheduler.solver.model_input import DAYS, OPERATING_SLOTS, WEEKEND_DAYS

# The boxes each day has on the master schedule sheet, left to right:
# weekdays two assistant boxes then two tech boxes (the 2 + 2 coverage
# target), weekends a single tech box.
WEEKDAY_BOXES = ("assistant", "assistant", "tech", "tech")
WEEKEND_BOXES = ("tech",)

DAY_HEADINGS = {"Mon": "Mon", "Tue": "Tue", "Wed": "Wed", "Thu": "Thurs", "Fri": "Fri", "Sat": "Sat", "Sun": "Sun"}

# Rows of the sheet's blank evening area that hold the colour key, and
# the boxes it covers: Fri's last two and Sat's.
KEY_ROWS = {20: "Key", 21: "Assistant Role", 22: "Technician/Hybrid"}
KEY_BOXES = (("Fri", 2), ("Fri", 3), ("Sat", 0))


def build_schedule_grid(solve_result: dict) -> pd.DataFrame:
    grid = pd.DataFrame("", index=TIME_SLOTS, columns=DAYS)

    for day in DAYS:
        open_slots = set(OPERATING_SLOTS[day])
        for i, label in enumerate(TIME_SLOTS):
            if i not in open_slots:
                grid.loc[label, day] = "Closed"

    for assignment in solve_result["assignments"]:
        # Initials are optional on Person (tests often skip them).
        initials = assignment["initials"] or assignment["name"]
        label = TIME_SLOTS[assignment["slot"]]
        day = assignment["day"]
        existing = grid.loc[label, day]
        grid.loc[label, day] = f"{existing}, {initials}" if existing else initials

    return grid


def build_hours_table(solve_result: dict) -> pd.DataFrame:
    """One row per person: requested vs. assigned hours."""
    return pd.DataFrame(
        [
            {
                "Name": p["name"],
                "Initials": p["initials"],
                "Hours requested": p["hours_requested"],
                "Hours assigned": p["hours_assigned"],
            }
            for p in solve_result["people"]
        ],
        columns=["Name", "Initials", "Hours requested", "Hours assigned"],
    )


def _clock(slot: int) -> str:
    """A slot's start on a 12-hour clock without am/pm, as the sheet
    writes it: 0 -> "8:00", 10 -> "1:00"."""
    hour, minute = divmod(slot, 2)
    hour = (8 + hour - 1) % 12 + 1
    return f"{hour}:{minute * 30:02d}"


def sheet_time_label(slot: int) -> str:
    """The time column's label: "Noon" for 12:00, otherwise _clock()."""
    return "Noon" if slot == 8 else _clock(slot)


def _fill_boxes(width: int, people: list[tuple[int, str]], previous: list[int | None]) -> list:
    """Put one role's people into `width` boxes. Anyone who was in a box
    the half hour before stays in it, so a shift reads as one unbroken
    column, as it does on the paper schedule."""
    boxes: list = [None] * width
    newcomers = []
    for person in people:
        if person[0] in previous and boxes[previous.index(person[0])] is None:
            boxes[previous.index(person[0])] = person
        else:
            newcomers.append(person)
    for person in newcomers:
        if None in boxes:
            boxes[boxes.index(None)] = person
        else:
            boxes.append(person)  # over the 2 + 2 cap; the solver never does this
    return boxes


def build_master_sheet(solve_result: dict) -> dict:
    """The schedule as the desk's master schedule sheet lays it out.

    Returns the day headings and one row per TIME_SLOTS entry. Each row's
    "cells" are the table cells left to right after the time column:
      box     - one person position: day, slot, role, person_id, initials
                (person_id None when nobody's in it); the day's first and
                last box also carry first/last
      closed  - the "Closed" bar under a day's last open half hour
      before  - a weekend's closed morning, as one block (`rowspan` rows)
      blank   - the empty sheet below a day's "Closed" bar
      key     - a line of the colour key
    plus "people", everyone in the result as (name, initials), split in
    two for the sheet's two name lists.
    """
    working = defaultdict(list)  # (day, slot, role) -> [(person_id, initials)]
    for a in solve_result["assignments"]:
        working[a["day"], a["slot"], a["role"]].append((a["person_id"], a["initials"] or a["name"]))

    days = []
    columns = {}  # day -> one list of cells per slot
    for day in DAYS:
        roles = WEEKEND_BOXES if day in WEEKEND_DAYS else WEEKDAY_BOXES
        open_slots = OPERATING_SLOTS[day]
        days.append(
            {
                "day": day,
                "heading": DAY_HEADINGS[day],
                "hours": f"{_clock(open_slots.start)}-{_clock(open_slots.stop)}",
                "width": len(roles),
            }
        )

        cells = []
        previous: list[int | None] = [None] * len(roles)
        for slot in range(len(TIME_SLOTS)):
            if slot in open_slots:
                boxes = []
                for role in dict.fromkeys(roles):
                    indexes = [i for i, r in enumerate(roles) if r == role]
                    people = sorted(working[day, slot, role], key=lambda p: p[1])
                    filled = _fill_boxes(len(indexes), people, [previous[i] for i in indexes])
                    boxes += [
                        {
                            "kind": "box",
                            "day": day,
                            "slot": slot,
                            "role": role,
                            "person_id": person[0] if person else None,
                            "initials": person[1] if person else "",
                        }
                        for person in filled
                    ]
                previous = [box["person_id"] for box in boxes]
                # Each day is ruled off from the next, as on the sheet.
                boxes[0]["first"] = boxes[-1]["last"] = True
                cells.append(boxes)
            elif slot == open_slots.stop:
                cells.append([{"kind": "closed", "colspan": len(roles)}])
            elif slot == 0:
                cells.append([{"kind": "before", "colspan": len(roles), "rowspan": open_slots.start}])
            elif slot < open_slots.start:
                cells.append([])  # inside the block above
            else:
                cells.append([{"kind": "blank", "day": day, "index": i} for i in range(len(roles))])
        columns[day] = cells

    rows = []
    for slot in range(len(TIME_SLOTS)):
        cells = [cell for day in DAYS for cell in columns[day][slot]]
        if slot in KEY_ROWS:
            cells = _with_key(cells, KEY_ROWS[slot], slot)
        # A heavier rule above each whole hour, as on the sheet.
        rows.append({"time": sheet_time_label(slot), "hour": slot % 2 == 0, "cells": cells})

    people = [(p["name"], p["initials"]) for p in solve_result["people"]]
    half = math.ceil(len(people) / 2)
    return {"days": days, "rows": rows, "people": [people[:half], people[half:]]}


def _with_key(cells: list[dict], text: str, slot: int) -> list[dict]:
    """Merge the blank boxes KEY_BOXES names into one key cell."""
    covered = [i for i, c in enumerate(cells) if c["kind"] == "blank" and (c["day"], c["index"]) in KEY_BOXES]
    if len(covered) != len(KEY_BOXES):
        return cells
    key = {"kind": "key", "text": text, "colspan": len(covered), "swatch": {21: "assistant", 22: "tech"}.get(slot)}
    return cells[: covered[0]] + [key] + cells[covered[-1] + 1 :]
