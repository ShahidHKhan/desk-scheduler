"""
Reading hours requested off a submitted form, and what happens when they
can't be read or don't fit the roster: the hours already on a returning
person's row are kept, and a row left without hours blocks solving until
the boss sets them.
"""

import openpyxl
import pytest

from scheduler.db import crud
from scheduler.ingest.schema import parse_hours
from scheduler.ingest.xlsx_parser import parse_xlsx
from scheduler.pipeline import runner


@pytest.mark.parametrize(
    "raw, hours",
    [
        (20, 20),
        (12.0, 12),
        ("20 Hrs.", 20),
        ("7 hrs", 7),
        ("~10 Hrs.", 10),
        ("10-14***", 14),  # a range reads as its upper end
        ("10-15 Hrs.", 15),
        ("6/8 hours", 8),
        ("*** Hrs.", None),  # the template's placeholder
        ("", None),
        (None, None),
    ],
)
def test_hours_as_people_type_them(raw, hours):
    assert parse_hours(raw) == hours


def test_xlsx_form_reads_hours_and_a_name_between_asterisks(tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["C2"], sheet["J2"] = "*** Lorena de la Rocha ***", "20 Hrs."
    path = tmp_path / "form.xlsx"
    workbook.save(path)

    submission = parse_xlsx(str(path))
    assert (submission.name, submission.hours_requested) == ("Lorena de la Rocha", 20)
    assert submission.warnings == []


def test_unreadable_hours_keep_what_the_roster_has():
    person = crud.add_person("Jane Doe", "JD", "tech_only", 3, 1, 12)
    crud.upsert_from_submission(person.id, "Jane Doe", None, {})
    assert crud.get_person(person.id).hours_requested == 12


def test_hours_over_the_limit_wait_for_the_boss_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    runner.graph_app.cache_clear()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["C2"], sheet["J2"] = "Jacob Doe", "25 Hrs."
    path = tmp_path / "form.xlsx"
    workbook.save(path)

    try:
        runner.start_run([str(path)], [])
        runner.resume({"decisions": [{"index": 0, "action": "new"}]})
        _, pause = runner.current_state()
        assert pause["kind"] == "roster_incomplete"
        [row] = pause["incomplete_rows"]
        assert "hours_requested" in row["missing_fields"]
    finally:
        runner.graph_app().checkpointer.conn.close()
        runner.graph_app.cache_clear()


def test_a_row_without_hours_is_incomplete():
    person = crud.upsert_from_submission(None, "New Person", None, {}, initials="NP")
    crud.update_person(person.id, role_weighting="tech_only", experience_rating=2, proximity=2)
    [incomplete] = crud.list_incomplete_roster()
    assert incomplete.missing_fields == ["hours_requested"]
