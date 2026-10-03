"""
In-app availability form: the alternative to filling in the xlsx template.

The form shows one checkbox per half-hour row (08:00-19:30) per day. These
pure functions convert between that grid and the AvailabilitySubmission
shape every other ingestion path produces, so an in-app submission flows
through the same pipeline (name-match confirm, completeness gate, solve)
as an uploaded file.
"""

from scheduler.db.models import Submission
from scheduler.ingest.schema import DAYS, TIME_SLOTS, AvailabilitySubmission, hours_range_warning
from scheduler.solver.model_input import OPERATING_SLOTS

# The form's rows: every slot that could ever be open on any day. The last
# TIME_SLOTS entry (20:00) is the template's closing label, never a shift.
FORM_SLOTS: list[int] = list(range(len(TIME_SLOTS) - 1))
FORM_TIME_LABELS: list[str] = [TIME_SLOTS[i] for i in FORM_SLOTS]


def availability_from_grid(grid: dict[str, list[bool]]) -> tuple[dict[str, list[bool]], int]:
    """Convert the form's grid (day -> one bool per FORM_SLOTS row) into
    full-length availability, keeping only slots the desk is open.

    Returns (availability, ignored) where `ignored` counts the boxes ticked
    outside opening hours (e.g. Saturday 09:00), so the form can say so
    instead of silently dropping them.
    """
    availability: dict[str, list[bool]] = {}
    ignored = 0
    for day in DAYS:
        rows = list(grid.get(day, []))
        flags = [False] * len(TIME_SLOTS)
        for row_index, slot in enumerate(FORM_SLOTS):
            ticked = bool(rows[row_index]) if row_index < len(rows) else False
            if not ticked:
                continue
            if slot in OPERATING_SLOTS[day]:
                flags[slot] = True
            else:
                ignored += 1
        availability[day] = flags
    return availability, ignored


def grid_from_availability(availability: dict[str, list[bool]]) -> dict[str, list[bool]]:
    """Inverse of availability_from_grid: the form rows for a stored availability."""
    return {
        day: [bool(availability.get(day, [False] * len(TIME_SLOTS))[slot]) for slot in FORM_SLOTS]
        for day in DAYS
    }


def available_hours(availability: dict[str, list[bool]]) -> float:
    """Total hours marked available across the week (each slot is 0.5 h)."""
    return sum(sum(flags) for flags in availability.values()) * 0.5


def to_availability_submission(record: Submission) -> AvailabilitySubmission:
    """Turn a stored in-app Submission into the pipeline's common input shape."""
    availability = record.availability
    warnings = []
    range_warning = hours_range_warning(record.hours_requested)
    if range_warning:
        warnings.append(range_warning)
    marked = available_hours(availability)
    if marked < record.hours_requested:
        warnings.append(
            f"{record.name} asked for {record.hours_requested} h but only marked {marked:g} h available"
        )

    return AvailabilitySubmission(
        name=record.name,
        hours_requested=record.hours_requested,
        availability=availability,
        source_file=f"in-app form #{record.id}",
        parser_used="in_app",
        warnings=warnings,
        initials=record.initials,
        app_submission_id=record.id,
    )
