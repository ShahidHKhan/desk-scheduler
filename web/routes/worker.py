"""
The worker's page: the in-app alternative to the Excel template. A
submission is saved as pending; nothing touches the roster until the
manager includes it in a run and confirms the name match.
"""

import json

from fastapi import APIRouter, Depends, Form, Request

from scheduler.db import crud
from scheduler.db.crud import RosterValidationError
from scheduler.ingest.in_app import FORM_SLOTS, FORM_TIME_LABELS, availability_from_grid, available_hours
from scheduler.ingest.schema import DAYS, HOURS_MAX, HOURS_MIN
from scheduler.solver.model_input import OPERATING_SLOTS
from web.auth import require_worker
from web.views import is_htmx, render

router = APIRouter(dependencies=[Depends(require_worker)])

# open[row][col]: whether the desk is open at that form row on that day.
OPEN_MASK = [[slot in OPERATING_SLOTS[day] for day in DAYS] for slot in FORM_SLOTS]


def blank_grid() -> dict[str, list[bool]]:
    return {day: [False] * len(FORM_SLOTS) for day in DAYS}


def _parse_grid(raw: str) -> dict[str, list[bool]]:
    """The grid's hidden field: {day: [one bool per form row]}. Anything
    malformed counts as nothing marked."""
    try:
        value = json.loads(raw)
    except ValueError:
        return blank_grid()
    if not isinstance(value, dict):
        return blank_grid()
    return {day: [bool(v) for v in value.get(day, [])][: len(FORM_SLOTS)] for day in DAYS}


def _form(request: Request, *, messages=(), name="", initials="", hours=10, grid=None):
    context = {
        "messages": list(messages),
        "name": name,
        "initials": initials,
        "hours": hours,
        "grid": grid or blank_grid(),
        "days": DAYS,
        "times": FORM_TIME_LABELS,
        "open_mask": OPEN_MASK,
        "hours_min": HOURS_MIN,
        "hours_max": HOURS_MAX,
    }
    template = "partials/submit_form.html" if is_htmx(request) else "worker.html"
    return render(request, template, **context)


@router.get("/submit")
def submit_page(request: Request):
    return _form(request)


@router.post("/submit")
def submit(
    request: Request,
    name: str = Form(""),
    initials: str = Form(""),
    hours_requested: str = Form(""),
    grid: str = Form("{}"),
):
    grid_value = _parse_grid(grid)

    def retry(message: str):
        # Keep everything the worker typed and marked, so they only fix the problem.
        return _form(
            request,
            messages=[("error", message)],
            name=name,
            initials=initials,
            hours=hours_requested,
            grid=grid_value,
        )

    try:
        hours = int(hours_requested)
    except ValueError:
        return retry(f"Enter the hours you want per week, from {HOURS_MIN} to {HOURS_MAX}.")
    if not HOURS_MIN <= hours <= HOURS_MAX:
        return retry(f"Hours wanted must be between {HOURS_MIN} and {HOURS_MAX}.")

    availability, _ = availability_from_grid(grid_value)
    marked = available_hours(availability)
    if marked == 0:
        return retry("Mark at least one half hour you're available.")

    try:
        crud.create_submission(name, initials, hours, availability)
    except RosterValidationError as e:
        return retry(str(e))

    messages = [("success", f"Thanks, {name.strip()}. Your availability was submitted ({marked:g} hours marked).")]
    if marked < hours:
        messages.append(
            (
                "warning",
                f"You asked for {hours} hours but marked only {marked:g} hours available, "
                "so you can be scheduled for at most that.",
            )
        )
    return _form(request, messages=messages)
