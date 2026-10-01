"""
Shared synthetic-roster builders for the pytest suite.

Kept separate from conftest.py fixtures since most rule tests build a
SolverInput directly and never touch the DB at all.
"""

from scheduler.solver.model_input import NUM_SLOTS, Person


def full_week_availability() -> dict[str, list[bool]]:
    """Available every operating slot, Mon-Sun (matches OPERATING_SLOTS in
    model_input.py). Same shape used by solve.py's and graph.py's own
    self-tests."""
    weekday = [True] * 24 + [False]
    fri = [True] * 18 + [False] * 7
    weekend = [False] * 8 + [True] * 10 + [False] * 7
    return {
        "Mon": weekday, "Tue": weekday, "Wed": weekday, "Thu": weekday,
        "Fri": fri, "Sat": weekend, "Sun": weekend,
    }


def no_availability() -> dict[str, list[bool]]:
    """Available nowhere, any day."""
    empty = [False] * NUM_SLOTS
    return {day: list(empty) for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}


def only_available(day: str, slots: range | list[int]) -> dict[str, list[bool]]:
    """Available in exactly `slots` on `day`, nowhere else at all."""
    availability = no_availability()
    flags = [False] * NUM_SLOTS
    for s in slots:
        flags[s] = True
    availability[day] = flags
    return availability


def make_person(
    id: int,
    name: str,
    role_weighting: str = "tech_only",
    experience_rating: int = 2,
    proximity: int = 2,
    hours_requested: int = 15,
    availability: dict[str, list[bool]] | None = None,
) -> Person:
    return Person(
        id=id,
        name=name,
        role_weighting=role_weighting,
        experience_rating=experience_rating,
        proximity=proximity,
        hours_requested=hours_requested,
        availability=availability if availability is not None else full_week_availability(),
    )


def generous_roster(tech_count: int = 4, assistant_count: int = 4, hours_requested: int = 20) -> list[Person]:
    """A roster with enough tech + assistant coverage, all available all
    week, to keep weekend coverage (hard) satisfiable and weekday coverage
    (soft) close to its 2+2 target - a baseline other tests extend with one
    deliberately-constrained person to exercise a specific rule, without
    every test having to re-derive "how much coverage is enough"."""
    people = []
    for i in range(1, tech_count + 1):
        people.append(make_person(i, f"Tech{i}", "tech_only", hours_requested=hours_requested))
    for i in range(tech_count + 1, tech_count + assistant_count + 1):
        people.append(make_person(i, f"Asst{i}", "assistant_only", hours_requested=hours_requested))
    return people
