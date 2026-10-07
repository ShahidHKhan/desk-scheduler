"""
Shared data shapes for the ingestion pipeline.

Every parser (xlsx, pdf, future vision fallback) produces the same
AvailabilitySubmission shape, so the rest of the app (roster linking,
solver input) doesn't care which parser produced it.
"""

import re
from dataclasses import dataclass, field

# The grid is fixed by the master schedule design: 25 half-hour slots,
# 8:00 AM through 8:00 PM (last slot label is the 8:00 PM start time).
# Hardcoded rather than parsed from the sheet, because the source sheet's
# own time column is inconsistently typed (datetime.time objects mixed
# with a literal "Noon" string) - simpler and more robust to fix the
# canonical slot list once here.
TIME_SLOTS: list[str] = [
    "08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30",
    "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30",
    "20:00",
]

DAYS: list[str] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Values that mean "not available" even though the cell isn't blank -
# template labels/metadata, not an actual availability mark.
NON_AVAILABILITY_VALUES = {"", "closed", "tech only"}

# Must match the Roster.hours_requested CHECK constraint (models.py) - kept
# here too so parsers can flag out-of-range values at ingestion time,
# before they ever reach the roster table.
HOURS_MIN = 3
HOURS_MAX = 20


@dataclass
class AvailabilitySubmission:
    """One person's parsed availability submission."""

    name: str
    hours_requested: int | None
    # availability[day][slot_index] = True if marked available
    availability: dict[str, list[bool]] = field(default_factory=dict)
    source_file: str = ""
    parser_used: str = ""  # "xlsx" | "pdf_text" | "pdf_vision" | "in_app"
    warnings: list[str] = field(default_factory=list)
    # Only set for in-app submissions: the worker types their own initials
    # (file templates have no initials field), and the submissions-table id
    # lets the pipeline mark that row imported once it's committed.
    initials: str | None = None
    app_submission_id: int | None = None

    def available_slots(self, day: str) -> list[str]:
        """Return the TIME_SLOTS labels this person marked available for a given day."""
        flags = self.availability.get(day, [])
        return [TIME_SLOTS[i] for i, avail in enumerate(flags) if avail]


def is_available_value(raw_value) -> bool:
    """Decide whether a cell's raw value means 'marked available'."""
    if raw_value is None:
        return False
    text = str(raw_value).strip().lower()
    return text not in NON_AVAILABILITY_VALUES


def parse_hours(raw_value) -> int | None:
    """The hours a form asks for, from whatever was typed in its hours
    box: 20, "20 Hrs.", "~10 Hrs.". A range such as "10-14" or "6/8" reads
    as its upper end - hours_requested is a ceiling (Rule 1), and the
    upper end is the most the person said they'd work. None when there's
    no number at all, as in the template's own "*** Hrs." placeholder."""
    if isinstance(raw_value, int | float):
        return int(raw_value)
    numbers = re.findall(r"\d+(?:\.\d+)?", str(raw_value or ""))
    if not numbers:
        return None
    return int(max(float(n) for n in numbers))


def hours_range_warning(hours_requested: int | None) -> str | None:
    """Return a warning string if hours_requested is outside HOURS_MIN..HOURS_MAX.

    Soft check only (returns a warning, doesn't raise) - the Roster table's
    own CHECK constraint hard-blocks out-of-range values later if/when the
    submission gets added to the roster, so this just flags it early for
    human review during ingestion instead of stopping the batch.
    """
    if hours_requested is None:
        return None
    if not (HOURS_MIN <= hours_requested <= HOURS_MAX):
        return (
            f"hours_requested={hours_requested} is outside the valid range "
            f"{HOURS_MIN}-{HOURS_MAX}"
        )
    return None
