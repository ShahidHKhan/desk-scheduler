"""
Input shape for the solver. Bridges Phase 1 (roster/models.py) and
Phase 2 (ingestion/schema.py's AvailabilitySubmission) into what the
CP-SAT model actually consumes.

Deliberately decoupled from the SQLAlchemy Roster model and the
ingestion parsers - the solver shouldn't need to know about databases
or file parsing, just plain data. Whatever wires Phase 1 + Phase 2
output into this shape (matching people by name/id) is a separate,
not-yet-built piece - see PHASE3_HANDOFF.md.
"""

from dataclasses import dataclass, field

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
    # master schedule's convention. Optional since roster/solver code that
    # predates Phase 5 doesn't set it.
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
    ever reaching the model (see PHASE5_DEV_INSTRUCTIONS.md).
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
