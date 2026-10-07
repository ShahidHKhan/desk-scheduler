"""
The web app end to end, through HTTP: a worker submits availability, and
the manager runs the pipeline, confirms the matches, edits boxes on the
schedule, approves and downloads it. Also adding a missed submission to a
run, the roster forms, dismissing submissions, file uploads, and full-page
versus HTMX responses.
"""

import io
import json
import re

import openpyxl
import pytest
from fastapi.testclient import TestClient

from scheduler.db import crud
from scheduler.ingest.in_app import FORM_SLOTS
from scheduler.ingest.schema import DAYS
from scheduler.pipeline import runner
from web.main import app
from web.routes.manager import FormError, _find_person

HX = {"HX-Request": "true"}
TECHS = ("Tech1", "Tech2")


@pytest.fixture(autouse=True)
def fresh_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))

    def restart():
        if runner.graph_app.cache_info().currsize:
            runner.graph_app().checkpointer.conn.close()
        runner.graph_app.cache_clear()

    restart()
    yield
    restart()


@pytest.fixture
def credentials(monkeypatch):
    for name, value in {
        "APP_USERNAME": "worker",
        "APP_PASSWORD": "worker-pass",
        "ADMIN_APP_USERNAME": "boss",
        "ADMIN_APP_PASSWORD": "boss-pass",
    }.items():
        monkeypatch.setenv(name, value)


def signed_in(username: str, password: str) -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"username": username, "password": password})
    return client


@pytest.fixture
def manager(credentials):
    with signed_in("boss", "boss-pass") as client:
        yield client


@pytest.fixture
def worker(credentials):
    with signed_in("worker", "worker-pass") as client:
        yield client


def weekend_grid() -> dict[str, list[bool]]:
    """The form's grid with Sat and Sun 12:00-17:00 marked (rows 8-17)."""
    return {day: [day in ("Sat", "Sun") and 8 <= row < 18 for row in FORM_SLOTS] for day in DAYS}


def weekend_availability() -> dict[str, list[bool]]:
    return {day: [day in ("Sat", "Sun") and 8 <= slot < 18 for slot in range(25)] for day in DAYS}


def seed_roster() -> dict[str, int]:
    return {name: crud.add_person(name, name[0] + name[-1], "tech_only", 3, 1, 10).id for name in TECHS}


def current_pause() -> dict | None:
    return runner.current_state()[1]


def working(day: str, slot: int) -> dict:
    """Who the schedule under review has in a weekend half hour (one tech)."""
    assignments = runner.current_state()[0]["solve_result"]["assignments"]
    [assignment] = [a for a in assignments if (a["day"], a["slot"]) == (day, slot)]
    return assignment


# --- the worker's form ---------------------------------------------------------


def test_worker_submits_availability(worker):
    page = worker.get("/submit")
    assert "data-availability-grid" in page.text

    form = {"name": "Jane Doe", "initials": "jd", "hours_requested": "12", "grid": json.dumps(weekend_grid())}
    response = worker.post("/submit", data=form, headers=HX)
    assert "Thanks, Jane Doe" in response.text
    assert "marked only 10 hours" in response.text  # 10 h marked, 12 wanted
    assert "<html" not in response.text  # just the form, swapped in place

    [submission] = crud.list_submissions("pending")
    assert (submission.name, submission.initials, submission.hours_requested) == ("Jane Doe", "JD", 12)
    assert submission.availability == weekend_availability()


@pytest.mark.parametrize(
    "hours, grid, error",
    [
        ("10", {}, "Mark at least one half hour"),
        ("40", weekend_grid(), "between 3 and 20"),
        ("", weekend_grid(), "Enter the hours you want"),
        ("10", "not json", "Mark at least one half hour"),
    ],
)
def test_worker_form_keeps_what_was_typed_when_something_is_wrong(worker, hours, grid, error):
    raw_grid = grid if isinstance(grid, str) else json.dumps(grid)
    form = {"name": "Jane Doe", "initials": "JD", "hours_requested": hours, "grid": raw_grid}
    response = worker.post("/submit", data=form, headers=HX)
    assert error in response.text
    assert 'value="Jane Doe"' in response.text
    assert crud.list_submissions("pending") == []


# --- a full run ----------------------------------------------------------------


def test_manager_runs_edits_approves_and_downloads(manager):
    seed_roster()
    submission_ids = [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]

    page = manager.get("/manager/import", headers=HX)
    assert all(name in page.text for name in TECHS)
    assert 'hx-swap-oob="true"' in page.text  # the tab bar rides along

    page = manager.post("/manager/import/run", data={"include": submission_ids}, headers=HX)
    assert "Confirm all" in page.text
    assert current_pause()["kind"] == "roster_confirm"

    page = manager.post("/manager/import/confirm", data={"action_0": "confirm", "action_1": "confirm"}, headers=HX)
    assert page.headers["HX-Push-Url"] == "/manager/review"
    assert "Approve" in page.text and "IT SERVICE DESK" in page.text and "Your edits" not in page.text
    assert all(s.status == "imported" for s in map(crud.get_submission, submission_ids))

    # Type the other tech's initials over whoever has Sat 13:00.
    there = working("Sat", 10)
    other = "Tech2" if there["name"] == "Tech1" else "Tech1"
    box = {"day": "Sat", "slot": "10", "role": "tech", "was": str(there["person_id"]), "initials": "t" + other[-1]}
    page = manager.post("/manager/review/box", data=box, headers=HX)
    assert f"{other}</strong> forced into tech on Sat 13:00" in page.text
    assert f"{there['name']}</strong> forced out of tech on Sat 13:00" in page.text
    assert working("Sat", 10)["name"] == other
    assert len(current_pause()["locks"]) == 2

    # Tech1 never marked Monday morning: rejected, and the first edit stays.
    bad = {"day": "Mon", "slot": "0", "role": "tech", "was": "", "initials": "T1"}
    page = manager.post("/manager/review/box", data=bad, headers=HX)
    assert "Lock for Tech1 on Mon 08:00" in page.text
    assert len(current_pause()["locks"]) == 2

    for lock in current_pause()["locks"]:
        remove = {k: lock[k] for k in ("person_id", "day", "slot", "role")}
        page = manager.post("/manager/review/remove", data=remove, headers=HX)
    assert current_pause()["locks"] == []
    assert "Your edits" not in page.text

    page = manager.post("/manager/review/decide", data={"decision": "approved"}, headers=HX)
    assert page.headers["HX-Push-Url"] == "/manager/output"
    assert "Schedule approved and saved." in page.text
    assert crud.latest_schedule() is not None

    csv = manager.get("/manager/output.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    assert ",Sat," in csv.text.splitlines()[0]

    assert "Nothing is waiting for review" in manager.get("/manager/review").text


def test_confirm_can_pick_a_different_person(manager):
    ids = seed_roster()
    [submission_id] = [crud.create_submission("T. One", "XX", 10, weekend_availability()).id]
    manager.post("/manager/import/run", data={"include": [submission_id]}, headers=HX)

    manager.post("/manager/import/confirm", data={"action_0": "choose_other", "person_0": ids["Tech1"]}, headers=HX)
    assert crud.get_submission(submission_id).roster_id == ids["Tech1"]
    assert len(crud.list_roster()) == 2  # nobody new


def test_running_with_nothing_chosen_says_what_to_do(manager):
    page = manager.post("/manager/import/run", headers=HX)
    assert "Tick at least one submission or choose a file" in page.text
    assert current_pause() is None


def test_a_missed_submission_joins_the_run_and_edits_stay(manager):
    seed_roster()
    late = crud.add_person("Tech3", "T3", "tech_only", 3, 1, 10)
    first = [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]
    manager.post("/manager/import/run", data={"include": first}, headers=HX)
    manager.post("/manager/import/confirm", data={"action_0": "confirm", "action_1": "confirm"}, headers=HX)
    assert hours_of("Tech3") == 0  # no availability yet

    there = working("Sat", 10)
    box = {"day": "Sat", "slot": "10", "role": "tech", "was": str(there["person_id"]), "initials": ""}
    manager.post("/manager/review/box", data=box, headers=HX)
    edits = current_pause()["locks"]
    assert edits

    page = manager.get("/manager/import", headers=HX)
    assert "Missed someone?" in page.text

    [missed] = [crud.create_submission("Tech3", "XX", 10, weekend_availability()).id]
    page = manager.post("/manager/import/add", data={"include": [missed]}, headers=HX)
    assert current_pause()["kind"] == "roster_confirm"
    assert [c["parsed_name"] for c in current_pause()["candidates"]] == ["Tech3"]

    page = manager.post("/manager/import/confirm", data={"action_0": "confirm"}, headers=HX)
    assert page.headers["HX-Push-Url"] == "/manager/review"
    assert current_pause()["locks"] == edits
    assert working("Sat", 10)["person_id"] != there["person_id"]
    assert hours_of("Tech3") > 0
    assert crud.get_submission(missed).roster_id == late.id


def test_an_unreadable_added_file_keeps_the_schedule_and_later_edits_work(manager):
    seed_roster()
    first = [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]
    manager.post("/manager/import/run", data={"include": first}, headers=HX)
    manager.post("/manager/import/confirm", data={"action_0": "confirm", "action_1": "confirm"}, headers=HX)

    files = [("files", ("broken.xlsx", b"not a workbook", "application/octet-stream"))]
    page = manager.post("/manager/import/add", files=files, headers=HX)
    assert page.headers["HX-Push-Url"] == "/manager/review"
    assert "broken.xlsx" in page.text and "Your last change could not be applied" in page.text

    there = working("Sat", 10)
    other = "Tech2" if there["name"] == "Tech1" else "Tech1"
    box = {"day": "Sat", "slot": "10", "role": "tech", "was": str(there["person_id"]), "initials": other}
    manager.post("/manager/review/box", data=box, headers=HX)
    assert working("Sat", 10)["name"] == other


def test_adding_to_a_run_needs_one_waiting_for_review(manager):
    [submission_id] = [crud.create_submission("Tech1", "XX", 10, weekend_availability()).id]
    page = manager.post("/manager/import/add", data={"include": [submission_id]}, headers=HX)
    assert "only be added while a schedule is waiting for review" in page.text
    assert current_pause() is None


def hours_of(name: str) -> float:
    [person] = [p for p in current_pause()["people"] if p["name"] == name]
    return person["hours_assigned"]


# --- typing into a box --------------------------------------------------------

PEOPLE = [
    {"person_id": 1, "name": "Adam Swalha", "initials": "AS"},
    {"person_id": 2, "name": "Ali Swalha", "initials": "AS"},
    {"person_id": 3, "name": "Jane Doe", "initials": "JD"},
    {"person_id": 4, "name": "Jane Roe", "initials": "JR"},
]


@pytest.mark.parametrize(
    "typed, person_id",
    [("jd", 3), (" JR ", 4), ("ali swalha", 2), ("Adam", 1)],
)
def test_a_box_finds_people_by_initials_or_name(typed, person_id):
    assert _find_person(typed, PEOPLE)["person_id"] == person_id


@pytest.mark.parametrize(
    "typed, error",
    [
        ("as", "AS could be Adam Swalha or Ali Swalha. Type the person's name instead."),
        ("jane", "More than one person is called jane (Jane Doe, Jane Roe)"),
        ("ZZ", "Nobody on the roster has the initials or name 'ZZ'."),
    ],
)
def test_a_box_refuses_to_guess(typed, error):
    with pytest.raises(FormError, match=re.escape(error)):
        _find_person(typed, PEOPLE)


def test_semester_and_desktop_support_are_kept(manager):
    response = manager.post("/manager/review/notes", data={"semester": " Fall 2026 "}, headers=HX)
    assert response.status_code == 204
    manager.post("/manager/review/notes", data={"desktop_support": "Pat Lee  Wed 2-5"}, headers=HX)
    assert crud.get_settings() == {"semester": "Fall 2026", "desktop_support": "Pat Lee  Wed 2-5"}


# --- uploads ------------------------------------------------------------------


def xlsx_bytes(name: str) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["C2"], sheet["J2"] = name, 10
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_upload_keeps_only_the_files_own_name(manager):
    files = [("files", ("../../outside.xlsx", xlsx_bytes("Tech1"), "application/octet-stream"))]
    page = manager.post("/manager/import/run", files=files, headers=HX)
    assert "From file outside.xlsx" in page.text
    [candidate] = current_pause()["candidates"]
    assert candidate["source"].endswith("outside.xlsx")
    assert ".." not in candidate["source"]


def test_upload_refuses_other_file_types(manager):
    files = [("files", ("notes.txt", b"hello", "text/plain"))]
    page = manager.post("/manager/import/run", files=files, headers=HX)
    assert "Only .xlsx and .pdf files can be read: notes.txt" in page.text
    assert current_pause() is None


# --- roster and submissions ---------------------------------------------------


def test_roster_add_save_and_remove(manager):
    person = {
        "name": "Jane Doe",
        "initials": "jd",
        "role_weighting": "",
        "experience_rating": "3",
        "proximity": "1",
        "hours_requested": "12",
    }
    page = manager.post("/manager/roster", data=person, headers=HX)
    assert "Choose a role weighting" in page.text
    assert crud.list_roster() == []

    page = manager.post("/manager/roster", data={**person, "role_weighting": "hybrid_new"}, headers=HX)
    assert "Added Jane Doe." in page.text
    [jane] = crud.list_roster()
    assert (jane.initials, jane.experience_rating, jane.proximity) == ("JD", 3, 1)
    assert "1 · On campus" in page.text and "4 · Most experienced" in page.text

    saved = {**person, "name": "Jane Smith", "role_weighting": "tech_only", "hours_requested": "8"}
    page = manager.post(f"/manager/roster/{jane.id}", data=saved, headers=HX)
    assert "Saved Jane Smith." in page.text
    assert (crud.get_person(jane.id).name, crud.get_person(jane.id).hours_requested) == ("Jane Smith", 8)

    page = manager.post(f"/manager/roster/{jane.id}", data={**saved, "hours_requested": "99"}, headers=HX)
    assert "Hours requested must be from 3 to 20." in page.text

    page = manager.post(f"/manager/roster/{jane.id}/delete", headers=HX)
    assert "Removed Jane Smith." in page.text
    assert crud.list_roster() == []


def test_roster_filter_shows_incomplete_people(manager):
    seed_roster()
    crud.upsert_from_submission(None, "New Person", 10)
    page = manager.get("/manager/roster?incomplete=true", headers=HX)
    assert "New Person" in page.text and "Tech1" not in page.text
    assert "1 person is missing required fields" in page.text


def test_dismissing_submissions(manager):
    keep, drop = (crud.create_submission(n, "XX", 10, weekend_availability()).id for n in ("Keep", "Drop"))
    page = manager.post("/manager/import/dismiss", data={"include": [drop]}, headers=HX)
    assert "Dismissed 1 submission." in page.text
    assert [s.id for s in crud.list_submissions("pending")] == [keep]


# --- page shape ---------------------------------------------------------------


@pytest.mark.parametrize("tab", ["roster", "import", "review", "output"])
def test_every_tab_renders_as_a_page_and_as_a_panel(manager, tab):
    full = manager.get(f"/manager/{tab}")
    assert full.status_code == 200 and "<html" in full.text and 'id="panel"' in full.text

    partial = manager.get(f"/manager/{tab}", headers=HX)
    assert partial.status_code == 200 and "<html" not in partial.text


def test_reset_is_off_unless_enabled(manager, monkeypatch):
    monkeypatch.delenv("ENABLE_RESET", raising=False)
    seed_roster()
    assert "Reset everything" not in manager.get("/manager/output", headers=HX).text
    assert manager.post("/manager/reset", headers=HX).status_code == 404
    assert len(crud.list_roster()) == 2


def test_reset_starts_over(manager, monkeypatch):
    monkeypatch.setenv("ENABLE_RESET", "true")
    seed_roster()
    crud.set_setting("semester", "Fall 2026")
    submission_ids = [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]
    manager.post("/manager/import/run", data={"include": submission_ids}, headers=HX)
    assert current_pause() is not None

    assert "Reset everything" in manager.get("/manager/output", headers=HX).text
    page = manager.post("/manager/reset", headers=HX)
    assert "Everything was reset." in page.text
    assert page.headers["HX-Push-Url"] == "/manager/import"
    assert current_pause() is None and runner.current_state()[0] == {}
    assert crud.list_roster() == [] and crud.list_submissions(None) == []
    assert crud.get_settings()["semester"] == ""


def test_csv_before_any_approval_is_not_found(manager):
    assert manager.get("/manager/output.csv").status_code == 404
