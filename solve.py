"""
Runs the solver and extracts a readable schedule from the solution.
"""

import os

from ortools.sat.python import cp_model

from build_model import build_model
from diagnose import diagnose_coverage_gaps, summarize_coverage_shortfalls
from model_input import DAYS, OPERATING_SLOTS, SolverInput

# Parallel search workers - safe, free speedup (PHASE3_HANDOFF.md
# "Performance note"). Capped at 8 since CP-SAT's returns diminish past
# that for a model this size, and we don't want to hog every core on a
# shared machine.
NUM_SEARCH_WORKERS = min(8, os.cpu_count() or 1)


def solve(data: SolverInput, time_limit_seconds: float = 30.0) -> dict:
    # Cheap pre-check before spending the full time budget on a model
    # that's guaranteed infeasible from a plain headcount gap (see
    # diagnose.py). Doesn't prove feasibility - only catches this one
    # clear-cut cause.
    coverage_gaps = diagnose_coverage_gaps(data)
    if coverage_gaps:
        return {
            "status": "INFEASIBLE",
            "feasible": False,
            "assignments": [],
            "hours_assigned": {},
            "coverage_gaps": coverage_gaps,
            "coverage_shortfalls": [],
        }

    model, variables = build_model(data)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = NUM_SEARCH_WORKERS
    status = solver.Solve(model)

    result = {
        "status": solver.StatusName(status),
        "feasible": status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
        "assignments": [],
        "hours_assigned": {},
        "coverage_gaps": [],
        # Weekday coverage is a soft constraint (build_model.py) - a
        # feasible solve can still leave weekday slots understaffed
        # relative to the 2+2 target. Distinct from coverage_gaps, which
        # only ever holds guaranteed-infeasible (weekend) gaps: this is
        # informational on an otherwise-successful solve, not a failure.
        "coverage_shortfalls": [],
    }

    if not result["feasible"]:
        return result

    x = variables["x"]

    for p in data.people:
        assigned_slots = 0
        for day in DAYS:
            for slot in OPERATING_SLOTS[day]:
                for role in ("assistant", "tech"):
                    if solver.Value(x[p.id, day, slot, role]):
                        result["assignments"].append(
                            {"person": p.name, "day": day, "slot": slot, "role": role}
                        )
                        assigned_slots += 1
        result["hours_assigned"][p.name] = assigned_slots * 0.5

    result["coverage_shortfalls"] = summarize_coverage_shortfalls(solver, variables)

    return result


if __name__ == "__main__":
    from model_input import Person

    # Realistic-scale synthetic test - NOT real data, but sized like
    # the actual project (~20 people) rather than a toy example, since
    # a smaller headcount genuinely cannot cover a full week's coverage
    # demand no matter how the constraints are tuned (a 4-6hr max block
    # means a handful of people simply can't cover 2 tech + 2 assistant
    # across 5.5 operating days - this isn't a modeling bug, it's
    # arithmetic: weekly tech-slot demand alone is ~248 slot-units).
    # 10 tech-capable + 10 assistant-only, hours_requested=15 each,
    # available all week.
    NUM_SLOTS = 25
    available_weekday = [True] * 24 + [False]
    available_fri = [True] * 18 + [False] * 7
    available_weekend = [False] * 8 + [True] * 10 + [False] * 7  # slots 8-17 = 12:00-17:00

    def full_week_availability():
        return {
            "Mon": available_weekday, "Tue": available_weekday,
            "Wed": available_weekday, "Thu": available_weekday,
            "Fri": available_fri, "Sat": available_weekend, "Sun": available_weekend,
        }

    people = []
    for i in range(1, 11):
        people.append(Person(i, f"Tech{i}", "tech_only", 3, 1, 15, full_week_availability()))
    for i in range(11, 21):
        people.append(Person(i, f"Asst{i}", "assistant_only", 3, 1, 15, full_week_availability()))

    data = SolverInput(people=people)
    result = solve(data, time_limit_seconds=10)

    print("Status:", result["status"])
    if result["coverage_gaps"]:
        print(f"{len(result['coverage_gaps'])} coverage gap(s):")
        for gap in result["coverage_gaps"]:
            print(" ", gap)
    print("Hours assigned:", result["hours_assigned"])
    print(f"{len(result['assignments'])} slot-assignments made")
