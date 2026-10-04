"""
Input shape for the solver: what the CP-SAT model actually consumes.

Deliberately decoupled from the SQLAlchemy Roster model and the
ingestion parsers - the solver shouldn't need to know about databases
or file parsing, just plain data. pipeline/graph.py's
_build_solver_input() converts roster rows into this shape.
"""

from dataclasses import dataclass, field

from scheduler.ingest.schema import TIME_SLOTS

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NUM_SLOTS = 25  # matches ingestion/schema.py TIME_SLOTS
ROLES = ["assistant", "tech"]

# Which slot indices the desk is actually open, per day - derived from
# Blank_Schedule.xlsx's own header row ("8:00-8:00" Mon-Thu, "8:00-5:00"
# Fri, "12:00-5:00" Sat/Sun) and confirmed against the template's own
# "Closed" markers on the trailing slot. Coverage constraints only apply
# within these ranges - outside them nobody's available anyway (the
# ingestion parser already reflects "Closed" as unavailable for
# everyone), but the coverage constraint itself needs to know NOT to
# demand "2 assistant + 2 tech" during hours nobody could possibly work.
OPERATING_SLOTS = {
    "Mon": range(0, 24),
    "Tue": range(0, 24),
    "Wed": range(0, 24),
    "Thu": range(0, 24),
    "Fri": range(0, 18),
    "Sat": range(8, 18),
    "Sun": range(8, 18),
}
WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri"}
WEEKEND_DAYS = {"Sat", "Sun"}


def time_label(slot: int) -> str:
    """The clock time a slot starts at: 4 -> "10:00". Falls back to
    "slot N" for an index off the grid, so a message about a bad lock can
    still name it."""
    return TIME_SLOTS[slot] if 0 <= slot < len(TIME_SLOTS) else f"slot {slot}"


def slot_label(day: str, slot: int) -> str:
    """How messages name a slot: ("Mon", 4) -> "Mon 10:00"."""
    return f"{day} {time_label(slot)}"


def operating_slot_options(day: str) -> list[tuple[int, str]]:
    """(slot, "10:00-10:30") for each half hour the desk is open on `day`,
    for a UI to offer instead of raw slot numbers."""
    return [(slot, f"{TIME_SLOTS[slot]}-{TIME_SLOTS[slot + 1]}") for slot in OPERATING_SLOTS[day]]


@dataclass
class Person:
    id: int
    name: str
    role_weighting: str  # "assistant_only" | "hybrid_new" | "hybrid_2nd" | "tech_only"
    experience_rating: int  # 1-4
    proximity: int  # 1-3
    hours_requested: int
    # availability[day][slot_index] = True if this person marked available
    availability: dict[str, list[bool]]
    # Compact initials for the output grid (e.g. "SK") - matches the real
    # master schedule's convention. Optional so the solver can run
    # without it (e.g. in tests).
    initials: str = ""

    def can_work_tech(self) -> bool:
        return self.role_weighting != "assistant_only"

    def is_available(self, day: str, slot: int) -> bool:
        return self.availability.get(day, [False] * NUM_SLOTS)[slot]


@dataclass
class LockedAssignment:
    """A manual override forcing a person into or out of a specific slot/role.

    Injected as a hard constraint in build_model.py - always wins over the
    solver's own preferences. Validated by locks.validate_locks() before
    ever reaching the model.
    """

    person_id: int
    day: str
    slot: int
    role: str  # "assistant" | "tech"
    value: bool  # True = force this person INTO this slot/role, False = force OUT


@dataclass
class SolverInput:
    people: list[Person]
    min_block_slots: int = 4   # 2 hours (confirmed decision)
    max_block_slots: int = 12  # 6 hours (hard constraint, Rule 7)
    locked_assignments: list[LockedAssignment] = field(default_factory=list)
