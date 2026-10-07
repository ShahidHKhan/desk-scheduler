"""
Runs the solver and extracts a readable schedule from the solution.
"""

import os

from ortools.sat.python import cp_model

from scheduler.solver.build_model import build_model
from scheduler.solver.diagnose import diagnose_coverage_gaps, summarize_coverage_shortfalls
from scheduler.solver.model_input import DAYS, OPERATING_SLOTS, SolverInput

# Parallel search workers - safe, free speedup. Capped at 8 since CP-SAT's returns diminish past
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
            "people": [],
            "coverage_gaps": coverage_gaps,
            "coverage_shortfalls": [],
        }

    model, variables = build_model(data)

    # Pass 1: coverage, then fairness. Gets up to half the time budget,
    # and hands back whatever it doesn't use.
    solver = _solver(time_limit_seconds / 2)
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solver = _tidy_shifts(model, variables, solver, time_limit_seconds - solver.WallTime())

    result = {
        "status": solver.StatusName(status),
        "feasible": status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
        # Both keyed by person id, never by name - names aren't unique on
        # the roster. Name and initials ride along for display, as they
        # were at solve time, so a saved schedule needs no roster lookup.
        # assignments: {person_id, name, initials, day, slot, role}
        # people: {person_id, name, initials, hours_requested, hours_assigned}
        "assignments": [],
        "people": [],
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
                            {
                                "person_id": p.id,
                                "name": p.name,
                                "initials": p.initials,
                                "day": day,
                                "slot": slot,
                                "role": role,
                            }
                        )
                        assigned_slots += 1
        result["people"].append(
            {
                "person_id": p.id,
                "name": p.name,
                "initials": p.initials,
                "hours_requested": p.hours_requested,
                "hours_assigned": assigned_slots * 0.5,
            }
        )

    result["coverage_shortfalls"] = summarize_coverage_shortfalls(solver, variables)

    return result


def _solver(time_limit_seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = NUM_SEARCH_WORKERS
    return solver


def _tidy_shifts(
    model: cp_model.CpModel, variables: dict, first_pass: cp_model.CpSolver, time_limit_seconds: float
) -> cp_model.CpSolver:
    """Pass 2: hold coverage and the worst-off person's hours at what pass
    1 found, and minimize the shift-shape penalty (role switches, split
    days, short shifts - see build_model.py) within that. Starts from pass
    1's schedule, so it can only improve on it; if it finds nothing in
    time, pass 1's answer stands."""
    model.Add(variables["total_coverage_shortfall"] <= first_pass.Value(variables["total_coverage_shortfall"]))
    model.Add(variables["max_shortfall_permille"] <= first_pass.Value(variables["max_shortfall_permille"]))
    model.ClearHints()
    for i, value in enumerate(first_pass.ResponseProto().solution):
        model.AddHint(model.GetIntVarFromProtoIndex(i), value)
    model.Minimize(variables["shape_penalty"])

    solver = _solver(max(time_limit_seconds, 1.0))
    status = solver.Solve(model)
    return solver if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else first_pass


if __name__ == "__main__":
    from scheduler.solver.model_input import Person

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
    print("Hours assigned:", {p["name"]: p["hours_assigned"] for p in result["people"]})
    print(f"{len(result['assignments'])} slot-assignments made")
