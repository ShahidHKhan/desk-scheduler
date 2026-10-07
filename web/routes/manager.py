"""
The manager's side: Roster, Import availability, Review & edit and Output
tabs.

Every tab is a panel template. A full page load renders it inside
manager.html; an HTMX request gets just the panel, plus the tab bar swapped
out-of-band so the active tab and the pending count stay current. Routes
are plain `def`s, so FastAPI runs them in a worker thread - a solve can
take up to 30 seconds.
"""

import os
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
from scheduler.pipeline.format_output import build_hours_table, build_master_sheet, build_schedule_grid
from scheduler.pipeline.graph import REVIEW, ROSTER_CONFIRM
from scheduler.solver.model_input import DAYS, ROLES, WEEKEND_DAYS, slot_label
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
    return {
        "sheet": build_master_sheet(result),
        "settings": crud.get_settings(),
        "hours": build_hours_table(result).to_dict("records"),
        "shortfalls": result.get("coverage_shortfalls") or [],
    }


def _find_person(text: str, people: list[dict]) -> dict:
    """The person `text` names, out of a solve result's people: by
    initials, else by full name, else by first name. Two people can share
    initials (or a first name), so a match has to be the only one."""
    wanted = text.strip().lower()
    by_initials = [p for p in people if (p["initials"] or "").lower() == wanted]
    if len(by_initials) > 1:
        names = " or ".join(p["name"] for p in by_initials)
        raise FormError(f"{text.strip().upper()} could be {names}. Type the person's name instead.")
    matches = (
        by_initials
        or [p for p in people if p["name"].lower() == wanted]
        or [p for p in people if p["name"].split()[0].lower() == wanted]
    )
    if len(matches) > 1:
        names = ", ".join(p["name"] for p in matches)
        raise FormError(f"More than one person is called {text.strip()} ({names}). Type their initials or full name.")
    if not matches:
        raise FormError(f"Nobody on the roster has the initials or name {text.strip()!r}.")
    return matches[0]


def _save_uploads(files: list[UploadFile]) -> list[str]:
    """Write the uploaded forms to a temporary folder and return their
    paths. A file input with nothing chosen still sends one empty part,
    which is skipped."""
    uploads = [f for f in files if f.filename]
    bad = [f.filename for f in uploads if Path(f.filename).suffix.lower() not in UPLOAD_SUFFIXES]
    if bad:
        raise FormError(f"Only .xlsx and .pdf files can be read: {', '.join(bad)}")

    tmpdir = Path(tempfile.mkdtemp())
    paths = []
    for upload in uploads:
        # The browser supplies the name; keep only its last part so it
        # can't point outside tmpdir.
        path = tmpdir / Path(upload.filename).name
        path.write_bytes(upload.file.read())
        paths.append(str(path))
    return paths


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


def _pending_context() -> dict:
    """The in-app submissions waiting to be included in a run."""
    pending = crud.list_submissions("pending")
    names = [s.name.lower() for s in pending]
    return {
        "pending": [
            {
                "id": s.id,
                "name": s.name,
                "initials": s.initials,
                "hours_requested": s.hours_requested,
                "hours_available": available_hours(s.availability),
                "submitted": local_time(s.submitted_at),
            }
            for s in pending
        ],
        "duplicate_names": sorted({s.name for s in pending if names.count(s.name.lower()) > 1}),
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
    elif kind == REVIEW:
        # Importing is done, but a forgotten form can still join this run.
        context.update(_pending_context())
    elif kind is None:
        # A pause checkpointed before payloads had a "kind" lands here too,
        # and starting a new run replaces it.
        if values.get("review_notes") and values.get("solve_result") is None:
            context["failed_notes"] = values["review_notes"]
        context.update(_pending_context())
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
        **_schedule_tables(values["solve_result"]),
    }


def reset_enabled() -> bool:
    """Whether the Output tab offers to wipe everything. Only for testing,
    so it's off unless ENABLE_RESET=true (see .env.example)."""
    return os.environ.get("ENABLE_RESET", "").strip().lower() == "true"


def output_context() -> dict:
    schedule = crud.latest_schedule()
    if schedule is None:
        return {"schedule": None, "reset_enabled": reset_enabled()}
    return {
        "schedule": schedule,
        "approved": local_time(schedule.approved_at),
        "reset_enabled": reset_enabled(),
        **_schedule_tables(schedule.result),
    }


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


NOTHING_CHOSEN = "Tick at least one submission or choose a file to upload."


@router.post("/import/run")
def run_pipeline(
    request: Request,
    include: list[int] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
):
    try:
        paths = _save_uploads(files)
        if not paths and not include:
            raise FormError(NOTHING_CHOSEN)
    except FormError as e:
        return panel(request, "import", messages=[("error", str(e))])

    runner.start_run(paths, include)
    return panel(request, "import")


@router.post("/import/add")
def add_to_run(
    request: Request,
    include: list[int] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
):
    """Bring more submissions into a run that's waiting for review. They
    get the same name matching and roster check as the first ones, then
    the week is solved again, keeping the edits made so far."""
    _, pause = runner.current_state()
    if not pause or pause.get("kind") != REVIEW:
        message = "Submissions can only be added while a schedule is waiting for review."
        return panel(request, "import", messages=[("error", message)])
    try:
        paths = _save_uploads(files)
        if not paths and not include:
            raise FormError(NOTHING_CHOSEN)
    except FormError as e:
        return panel(request, "import", messages=[("error", str(e))])

    messages = _resume({"decision": "add", "file_paths": paths, "app_submission_ids": include})
    return _after_resume(request, messages)


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


@router.post("/review/box")
def edit_box(
    request: Request,
    day: str = Form(""),
    slot: str = Form(""),
    role: str = Form(""),
    was: str = Form(""),
    initials: str = Form(""),
):
    """The manager typed into one box of the schedule: a day, half hour
    and role. Whoever was in it is forced out of that half hour, whoever
    was typed is forced into the box, and the week is solved again around
    that."""
    _, pause = runner.current_state()
    if not pause or pause.get("kind") != REVIEW:
        return panel(request, "review")
    try:
        if day not in DAYS or role not in ROLES:
            raise FormError("That box isn't on the schedule.")
        slot_number = _int(slot, "Time", FORM_SLOTS[0], FORM_SLOTS[-1])
        typed = _find_person(initials, pause["people"]) if initials.strip() else None
    except FormError as e:
        return panel(request, "review", messages=[("error", str(e))])

    was_id = int(was) if was.isdigit() else None
    new_id = typed["person_id"] if typed else None
    if new_id == was_id:
        return panel(request, "review")

    locks = []
    if was_id is not None:
        # Out of both roles: forced out of only this box's role, they
        # could turn up in the other one in the same half hour.
        roles = ["tech"] if day in WEEKEND_DAYS else ROLES
        locks += [{"person_id": was_id, "day": day, "slot": slot_number, "role": r, "value": False} for r in roles]
    if new_id is not None:
        locks.append({"person_id": new_id, "day": day, "slot": slot_number, "role": role, "value": True})
    return panel(request, "review", messages=_resume({"decision": "edit", "add_locks": locks}))


@router.post("/reset")
def reset_everything(request: Request):
    """Start over from nothing: no run, roster, submissions, approved
    schedules or settings. For testing the import and roster steps again."""
    if not reset_enabled():
        raise HTTPException(status_code=404)
    runner.reset()
    crud.delete_everything()
    return panel(
        request,
        "import",
        messages=[("success", "Everything was reset. Import availability to start again.")],
        headers={"HX-Push-Url": "/manager/import"},
    )


@router.post("/review/notes")
def save_notes(semester: str | None = Form(None), desktop_support: str | None = Form(None)):
    """Save the master schedule's semester or Desktop Support text,
    whichever the request carries. Nothing on the page needs redrawing."""
    for key, value in (("semester", semester), ("desktop_support", desktop_support)):
        if value is not None:
            crud.set_setting(key, value.strip())
    return Response(status_code=204)


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
