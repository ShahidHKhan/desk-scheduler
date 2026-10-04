"""
Display tables for a solver result: the master-schedule grid (TIME_SLOTS x
DAYS, "Closed" outside operating hours - matching the real
Blank_Schedule.xlsx template's own convention) and per-person hours.

Both read only the result itself (see solver/solve.py), which carries
each person's name and initials, so they work the same for a schedule
saved months ago as for one just solved.
"""

import pandas as pd

from scheduler.ingest.schema import TIME_SLOTS
from scheduler.solver.model_input import DAYS, OPERATING_SLOTS


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
