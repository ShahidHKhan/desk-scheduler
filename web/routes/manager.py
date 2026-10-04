"""
The manager's side: Roster, Import availability, Review & edit and Output
tabs.

Every tab is a panel template. A full page load renders it inside
manager.html; an HTMX request gets just the panel, plus the tab bar swapped
out-of-band so the active tab and the pending count stay current. Routes
are plain `def`s, so FastAPI runs them in a worker thread - a solve can
take up to 30 seconds.
"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from scheduler.db import crud
from scheduler.db.crud import RosterValidationError
from scheduler.db.models import ROLE_WEIGHTINGS
from scheduler.ingest.in_app import FORM_SLOTS, available_hours
from scheduler.ingest.schema import HOURS_MAX, HOURS_MIN
from scheduler.pipeline import runner
from scheduler.pipeline.format_output import build_hours_table, build_schedule_grid
from scheduler.pipeline.graph import REVIEW, ROSTER_CONFIRM
from scheduler.solver.model_input import DAYS, ROLES, slot_label, time_label
from web.auth import require_admin
from web.views import is_htmx, local_time, render

router = APIRouter(prefix="/manager", dependencies=[Depends(require_admin)])

TABS = {"roster": "Roster", "import": "Import availability", "review": "Review & edit", "output": "Output"}

# What the ends of each rating scale mean. Rating 1 is the newest staff
# (the solver never pairs two of them on a weekday); proximity is how far
# from campus someone lives.
EXPERIENCE_LABELS = {1: "1 · Newest", 2: "2", 3: "3", 4: "4 · Most experienced"}
PROXIMITY_LABELS = {1: "1 · On campus", 2: "2 · In town", 3: "3 · Out of town"}

UPLOAD_SUFFIXES = {".xlsx", ".pdf"}


class FormError(ValueError):
    """A form value that can't be used; the message is shown to the manager."""


# --- shared helpers ---------------------------------------------------------


def _person_label(person) -> str:
    return f"{person.name} ({person.initials})" if person.initials else person.name


def _person_options() -> list[tuple[int, str]]:
    return [(p.id, _person_label(p)) for p in crud.list_roster()]


def _int(value: str, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise FormError(f"{label} must be a whole number from {low} to {high}.") from None
    if not low <= number <= high:
        raise FormError(f"{label} must be from {low} to {high}.")
    return number


def _resume(payload: dict) -> list[tuple[str, str]]:
    """Answer the pause the run is at. Returns messages to show: none on
    success, an error if nothing was paused."""
    try:
        runner.resume(payload)
    except runner.NoPausedRun as e:
        return [("error", f"{e} Start a new run from Import availability.")]
    return []


def _schedule_tables(result: dict) -> dict:
    grid = build_schedule_grid(result)
    return {
        "grid": {"days": list(grid.columns), "rows": [(time, list(cells)) for time, cells in grid.iterrows()]},
        "hours": build_hours_table(result).to_dict("records"),
        "shortfalls": result.get("coverage_shortfalls") or [],
    }


# --- what each tab shows ----------------------------------------------------


def roster_context(show_incomplete: bool = False) -> dict:
    roster = crud.list_roster()
    incomplete = [p for p in roster if not p.is_complete]
    return {
        "people": incomplete if show_incomplete else roster,
        "incomplete_count": len(incomplete),
        "show_incomplete": show_incomplete,
        "role_weightings": ROLE_WEIGHTINGS,
        "experience_labels": EXPERIENCE_LABELS,
        "proximity_labels": PROXIMITY_LABELS,
        "hours_min": HOURS_MIN,
        "hours_max": HOURS_MAX,
    }


def import_context() -> dict:
    values, pause = runner.current_state()
    kind = pause.get("kind") if pause else None
    context = {"kind": kind, "pause": pause}

    if kind == ROSTER_CONFIRM:
        context["candidates"] = [
            {**c, "source_name": Path(c.get("source") or "").name} for c in pause["candidates"]
        ]
        context["person_options"] = _person_options()
    elif kind is None:
        # A pause checkpointed before payloads had a "kind" lands here too,
        # and starting a new run replaces it.
        if values.get("review_notes") and values.get("solve_result") is None:
            context["failed_notes"] = values["review_notes"]
        pending = crud.list_submissions("pending")
        context["pending"] = [
            {
                "id": s.id,
                "name": s.name,
                "initials": s.initials,
                "hours_requested": s.hours_requested,
                "hours_available": available_hours(s.availability),
                "submitted": local_time(s.submitted_at),
            }
            for s in pending
        ]
        names = [s.name.lower() for s in pending]
        context["duplicate_names"] = sorted({s.name for s in pending if names.count(s.name.lower()) > 1})
    return context


def review_context() -> dict:
    values, pause = runner.current_state()
    if not pause or pause.get("kind") != REVIEW:
        return {"waiting": False, "has_approved": crud.latest_schedule() is not None}

    names = {p["person_id"]: p["name"] for p in pause["people"]}
    locks = [
        {
            **lock,
            "name": names.get(lock["person_id"], f"Person #{lock['person_id']}"),
            "where": slot_label(lock["day"], lock["slot"]),
            "key": {k: lock[k] for k in ("person_id", "day", "slot", "role")},
        }
        for lock in pause["locks"]
    ]
    return {
        "waiting": True,
        "pause": pause,
        "failed": bool(pause["lock_conflicts"] or pause["infeasibility_gaps"]),
        "locks": locks,
        "person_options": _person_options(),
        "days": DAYS,
        "slot_options": [(slot, time_label(slot)) for slot in FORM_SLOTS],
        "roles": ROLES,
        **_schedule_tables(values["solve_result"]),
    }


def output_context() -> dict:
    schedule = crud.latest_schedule()
    if schedule is None:
        return {"schedule": None}
    return {"schedule": schedule, "approved": local_time(schedule.approved_at), **_schedule_tables(schedule.result)}


CONTEXT = {"roster": roster_context, "import": import_context, "review": review_context, "output": output_context}


def panel(request: Request, tab: str, *, messages=(), headers: dict | None = None, **options):
    """Render one tab: the whole page on a normal load, just the panel (and
    the tab bar, out of band) for an HTMX request."""
    context = CONTEXT[tab](**options)
    context.update(
        tab=tab,
        tabs=TABS,
        pending_count=len(crud.list_submissions("pending")),
        messages=list(messages),
    )
    template = "partials/hx_panel.html" if is_htmx(request) else "manager.html"
    return render(request, template, headers=headers, **context)


def _after_resume(request: Request, messages: list) -> Response:
    """Where to go once a pause on the Import tab is answered: on to Review
    when the run reached it, otherwise back to Import."""
    _, pause = runner.current_state()
    if pause and pause.get("kind") == REVIEW:
        return panel(request, "review", messages=messages, headers={"HX-Push-Url": "/manager/review"})
    return panel(request, "import", messages=messages)


# --- pages --------------------------------------------------------------------


@router.get("")
def manager_home():
    return RedirectResponse("/manager/roster", status_code=303)


@router.get("/output.csv")
def download_csv():
    schedule = crud.latest_schedule()
    if schedule is None:
        raise HTTPException(status_code=404, detail="No approved schedule yet.")
    csv = build_schedule_grid(schedule.result).to_csv()
    return Response(
        csv, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="schedule.csv"'}
    )


@router.get("/{tab}")
def tab_page(request: Request, tab: str, incomplete: bool = False):
    if tab not in TABS:
        raise HTTPException(status_code=404)
    options = {"show_incomplete": incomplete} if tab == "roster" else {}
    return panel(request, tab, **options)


# --- roster -------------------------------------------------------------------


def _roster_fields(initials, role_weighting, experience_rating, proximity, hours_requested) -> dict:
    if role_weighting not in ROLE_WEIGHTINGS:
        raise FormError("Choose a role weighting. It's one of the four fields every person needs.")
    return {
        "initials": initials.strip().upper() or None,
        "role_weighting": role_weighting,
        "experience_rating": _int(experience_rating, "Experience rating", 1, 4),
        "proximity": _int(proximity, "Proximity", 1, 3),
        "hours_requested": _int(hours_requested, "Hours requested", HOURS_MIN, HOURS_MAX),
    }


@router.post("/roster")
def add_person(
    request: Request,
    name: str = Form(""),
    initials: str = Form(""),
    role_weighting: str = Form(""),
    experience_rating: str = Form("2"),
    proximity: str = Form("2"),
    hours_requested: str = Form("10"),
):
    try:
        if not name.strip():
            raise FormError("Enter the person's name.")
        fields = _roster_fields(initials, role_weighting, experience_rating, proximity, hours_requested)
        crud.add_person(name.strip(), **fields)
    except (FormError, RosterValidationError) as e:
        return panel(request, "roster", messages=[("error", str(e))])
    return panel(request, "roster", messages=[("success", f"Added {name.strip()}.")])


@router.post("/roster/{person_id}")
def save_person(
    request: Request,
    person_id: int,
    name: str = Form(""),
    initials: str = Form(""),
    role_weighting: str = Form(""),
    experience_rating: str = Form("2"),
    proximity: str = Form("2"),
    hours_requested: str = Form("10"),
    show_incomplete: bool = Form(False),
):
    try:
        if not name.strip():
            raise FormError("A person's name can't be empty.")
        fields = _roster_fields(initials, role_weighting, experience_rating, proximity, hours_requested)
        person = crud.update_person(person_id, name=name.strip(), **fields)
    except (FormError, RosterValidationError) as e:
        return panel(request, "roster", messages=[("error", str(e))], show_incomplete=show_incomplete)
    return panel(request, "roster", messages=[("success", f"Saved {person.name}.")], show_incomplete=show_incomplete)


@router.post("/roster/{person_id}/delete")
def delete_person(request: Request, person_id: int, show_incomplete: bool = Form(False)):
    person = crud.get_person(person_id)
    crud.delete_person(person_id)
    messages = [("success", f"Removed {person.name}.")] if person else []
    return panel(request, "roster", messages=messages, show_incomplete=show_incomplete)


# --- import -------------------------------------------------------------------


@router.post("/import/run")
def run_pipeline(
    request: Request,
    include: list[int] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
):
    # A file input with nothing chosen still sends one empty part.
    uploads = [f for f in files if f.filename]
    bad = [f.filename for f in uploads if Path(f.filename).suffix.lower() not in UPLOAD_SUFFIXES]
    if bad:
        message = f"Only .xlsx and .pdf files can be read: {', '.join(bad)}"
        return panel(request, "import", messages=[("error", message)])
    if not uploads and not include:
        message = "Tick at least one submission or choose a file to upload."
        return panel(request, "import", messages=[("error", message)])

    tmpdir = Path(tempfile.mkdtemp())
    paths = []
    for upload in uploads:
        # The browser supplies the name; keep only its last part so it
        # can't point outside tmpdir.
        path = tmpdir / Path(upload.filename).name
        path.write_bytes(upload.file.read())
        paths.append(str(path))

    runner.start_run(paths, include)
    return panel(request, "import")


@router.post("/import/dismiss")
def dismiss_submissions(request: Request, include: list[int] = Form(default=[])):
    if not include:
        return panel(request, "import", messages=[("error", "Tick the submissions to dismiss first.")])
    for submission_id in include:
        crud.set_submission_status(submission_id, "dismissed")
    noun = "submission" if len(include) == 1 else "submissions"
    return panel(request, "import", messages=[("success", f"Dismissed {len(include)} {noun}.")])


@router.post("/import/confirm")
async def confirm_matches(request: Request):
    # The form has one action per candidate (action_<index>, person_<index>),
    # so it's read whole rather than as declared parameters.
    form = await request.form()
    _, pause = await run_in_threadpool(runner.current_state)
    if not pause or pause.get("kind") != ROSTER_CONFIRM:
        return await run_in_threadpool(panel, request, "import")

    decisions = []
    for candidate in pause["candidates"]:
        index = candidate["index"]
        action = form.get(f"action_{index}", "new")
        if action == "choose_other":
            person = form.get(f"person_{index}")
            if not person or not str(person).isdigit():
                message = f"Choose which roster entry {candidate['parsed_name']} is."
                return await run_in_threadpool(panel, request, "import", messages=[("error", message)])
            decisions.append({"index": index, "action": "choose_other", "person_id": int(person)})
        elif action == "confirm" and candidate["suggested_match_id"] is not None:
            decisions.append({"index": index, "action": "confirm"})
        else:
            decisions.append({"index": index, "action": "new"})

    messages = await run_in_threadpool(_resume, {"decisions": decisions})
    return await run_in_threadpool(_after_resume, request, messages)


@router.post("/import/recheck")
def recheck_roster(request: Request):
    return _after_resume(request, _resume({}))


# --- review -------------------------------------------------------------------


@router.post("/review/decide")
def decide(request: Request, decision: str = Form("")):
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Unknown decision.")
    messages = _resume({"decision": decision})
    if decision == "approved" and not messages:
        return panel(
            request,
            "output",
            messages=[("success", "Schedule approved and saved.")],
            headers={"HX-Push-Url": "/manager/output"},
        )
    if not messages:
        messages = [("info", "Schedule rejected. Start a new run from Import availability when you're ready.")]
    return panel(request, "review", messages=messages)


@router.post("/review/edit")
def add_edit(
    request: Request,
    person_id: str = Form(""),
    day: str = Form(""),
    slot: str = Form(""),
    role: str = Form(""),
    force: str = Form("in"),
):
    try:
        if day not in DAYS or role not in ROLES:
            raise FormError("Choose a day and a role for the edit.")
        lock = {
            "person_id": _int(person_id, "Person", 1, 2**31),
            "day": day,
            "slot": _int(slot, "Time", FORM_SLOTS[0], FORM_SLOTS[-1]),
            "role": role,
            "value": force == "in",
        }
    except FormError as e:
        return panel(request, "review", messages=[("error", str(e))])
    return panel(request, "review", messages=_resume({"decision": "edit", "add_locks": [lock]}))


@router.post("/review/remove")
def remove_edit(
    request: Request,
    person_id: int = Form(...),
    day: str = Form(...),
    slot: int = Form(...),
    role: str = Form(...),
):
    lock = {"person_id": person_id, "day": day, "slot": slot, "role": role}
    return panel(request, "review", messages=_resume({"decision": "edit", "remove_locks": [lock]}))
