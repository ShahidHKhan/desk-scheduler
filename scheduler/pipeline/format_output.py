"""
Builds the master-schedule-grid view of a solver result: TIME_SLOTS x
DAYS, "Closed" outside operating hours - matching the real
Blank_Schedule.xlsx template's own convention.
"""

import pandas as pd

from scheduler.ingest.schema import TIME_SLOTS
from scheduler.solver.model_input import DAYS, OPERATING_SLOTS


def build_schedule_grid(solve_result: dict, people_lookup: dict) -> pd.DataFrame:
    """`people_lookup` maps whatever key solve_result["assignments"] uses
    (currently person name - see solve.py) to an object exposing `.initials`.
    """
    grid = pd.DataFrame("", index=TIME_SLOTS, columns=DAYS)

    for day in DAYS:
        open_slots = set(OPERATING_SLOTS[day])
        for i, label in enumerate(TIME_SLOTS):
            if i not in open_slots:
                grid.loc[label, day] = "Closed"

    for assignment in solve_result["assignments"]:
        person = people_lookup.get(assignment["person"])
        initials = person.initials if person is not None else assignment["person"]
        label = TIME_SLOTS[assignment["slot"]]
        day = assignment["day"]
        existing = grid.loc[label, day]
        grid.loc[label, day] = f"{existing}, {initials}" if existing else initials

    return grid
