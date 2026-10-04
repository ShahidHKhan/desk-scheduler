"""
The web app end to end, through HTTP: a worker submits availability, and
the manager runs the pipeline, confirms the matches, edits, approves and
downloads the schedule. Also the roster forms, dismissing submissions,
file uploads, and full-page versus HTMX responses.
"""

import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

from scheduler.db import crud
from scheduler.ingest.in_app import FORM_SLOTS
from scheduler.ingest.schema import DAYS
from scheduler.pipeline import runner
from web.main import app

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
    ids = seed_roster()
    submission_ids = [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]

    page = manager.get("/manager/import", headers=HX)
    assert all(name in page.text for name in TECHS)
    assert 'hx-swap-oob="true"' in page.text  # the tab bar rides along

    page = manager.post("/manager/import/run", data={"include": submission_ids}, headers=HX)
    assert "Confirm all" in page.text
    assert current_pause()["kind"] == "roster_confirm"

    page = manager.post("/manager/import/confirm", data={"action_0": "confirm", "action_1": "confirm"}, headers=HX)
    assert page.headers["HX-Push-Url"] == "/manager/review"
    assert "Approve" in page.text and "No edits yet" in page.text
    assert all(s.status == "imported" for s in map(crud.get_submission, submission_ids))

    edit = {"person_id": ids["Tech1"], "day": "Sat", "slot": "10", "role": "tech", "force": "in"}
    page = manager.post("/manager/review/edit", data=edit, headers=HX)
    assert "forced into tech on Sat 13:00" in page.text
    assert len(current_pause()["locks"]) == 1

    # Tech1 never marked Monday morning: rejected, and the first edit stays.
    bad = {**edit, "day": "Mon", "slot": "0"}
    page = manager.post("/manager/review/edit", data=bad, headers=HX)
    assert "Lock for Tech1 on Mon 08:00" in page.text
    assert len(current_pause()["locks"]) == 1

    remove = {k: edit[k] for k in ("person_id", "day", "slot", "role")}
    page = manager.post("/manager/review/remove", data=remove, headers=HX)
    assert current_pause()["locks"] == []
    assert "No edits yet" in page.text

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


def test_csv_before_any_approval_is_not_found(manager):
    assert manager.get("/manager/output.csv").status_code == 404
