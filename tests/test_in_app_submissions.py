"""
In-app availability submissions: the form-grid conversion, the
submissions table, and a full pipeline run that commits them to the roster.
"""

import pytest
from helpers import full_week_availability
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from scheduler.db import crud
from scheduler.db.crud import RosterValidationError
from scheduler.ingest.in_app import (
    FORM_SLOTS,
    availability_from_grid,
    available_hours,
    grid_from_availability,
    to_availability_submission,
)
from scheduler.ingest.schema import DAYS
from scheduler.pipeline.graph import build_graph


def empty_grid() -> dict[str, list[bool]]:
    return {day: [False] * len(FORM_SLOTS) for day in DAYS}


def test_grid_keeps_open_hours_and_counts_ignored_ticks():
    grid = empty_grid()
    grid["Mon"][0] = True   # 08:00 Mon - open
    grid["Sat"][0] = True   # 08:00 Sat - closed (weekends open 12:00)
    grid["Sat"][8] = True   # 12:00 Sat - open
    grid["Fri"][20] = True  # 18:00 Fri - closed (Fridays close 17:00)

    availability, ignored = availability_from_grid(grid)

    assert ignored == 2
    assert availability["Mon"][0] and availability["Sat"][8]
    assert not availability["Sat"][0] and not availability["Fri"][20]
    assert all(len(flags) == 25 for flags in availability.values())
    assert available_hours(availability) == 1.0


def test_grid_round_trips_through_availability():
    availability = full_week_availability()
    grid = grid_from_availability(availability)
    assert availability_from_grid(grid) == (availability, 0)


def test_create_and_dismiss_submission():
    created = crud.create_submission(" Jane Doe ", "jd", 12, full_week_availability())
    assert (created.name, created.initials, created.status) == ("Jane Doe", "JD", "pending")
    assert [s.id for s in crud.list_submissions("pending")] == [created.id]

    crud.set_submission_status(created.id, "dismissed")
    assert crud.list_submissions("pending") == []
    assert crud.list_submissions(None)[0].status == "dismissed"


@pytest.mark.parametrize(
    ("name", "initials", "hours"),
    [("", "JD", 10), ("Jane", " ", 10), ("Jane", "JD", 25)],
)
def test_create_submission_rejects_bad_input(name, initials, hours):
    with pytest.raises(RosterValidationError):
        crud.create_submission(name, initials, hours, full_week_availability())


def test_warns_when_fewer_hours_marked_than_requested():
    grid = empty_grid()
    for row in range(4):  # 2 hours on Monday morning
        grid["Mon"][row] = True
    availability, _ = availability_from_grid(grid)
    record = crud.create_submission("Sam Lee", "SL", 10, availability)

    submission = to_availability_submission(record)

    assert submission.parser_used == "in_app"
    assert submission.initials == "SL"
    assert submission.app_submission_id == record.id
    assert any("only marked 2 h" in w for w in submission.warnings)


def test_pipeline_commits_in_app_submissions_to_roster(tmp_path):
    # An existing person whose initials the manager hasn't set yet, and one
    # whose initials the manager already chose.
    no_initials = crud.upsert_from_submission(None, "Alex Kim", 8, full_week_availability())
    has_initials = crud.add_person("Jordan Park", "JPK", "tech_only", 3, 1, 10)

    subs = [
        crud.create_submission("alex kim", "AK", 12, full_week_availability()),
        crud.create_submission("Jordan Park", "JP", 15, full_week_availability()),
        crud.create_submission("New Person", "NP", 9, full_week_availability()),
    ]

    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as checkpointer:
        app = build_graph(checkpointer)
        config = {"configurable": {"thread_id": "in-app-test"}}

        result = app.invoke(
            {"submission_file_paths": [], "app_submission_ids": [s.id for s in subs]}, config=config
        )
        candidates = result["__interrupt__"][0].value["candidates"]
        assert all(c["from_app"] for c in candidates)
        matched = {c["parsed_name"]: c["suggested_match_id"] for c in candidates}
        assert matched == {"alex kim": no_initials.id, "Jordan Park": has_initials.id, "New Person": None}

        decisions = [
            {"index": c["index"], "action": "confirm" if c["suggested_match_id"] else "new"} for c in candidates
        ]
        result = app.invoke(Command(resume={"decisions": decisions}), config=config)

        # Alex (no role/rating/proximity) and New Person are incomplete, so the
        # run now waits at the completeness gate - the manager's part.
        incomplete = {row["name"] for row in result["__interrupt__"][0].value["incomplete_rows"]}
        assert incomplete == {"Alex Kim", "New Person"}

    roster = {p.name: p for p in crud.list_roster()}
    assert roster["Alex Kim"].initials == "AK"            # filled in, was unset
    assert roster["Alex Kim"].hours_requested == 12
    assert roster["Jordan Park"].initials == "JPK"        # manager's choice kept
    assert roster["Jordan Park"].hours_requested == 15
    assert roster["New Person"].initials == "NP"
    assert roster["New Person"].missing_fields == ["role_weighting", "experience_rating", "proximity"]

    for sub in subs:
        stored = crud.get_submission(sub.id)
        assert stored.status == "imported"
        assert stored.roster_id == roster[{"alex kim": "Alex Kim"}.get(sub.name, sub.name)].id
    assert crud.list_submissions("pending") == []


def test_already_imported_submission_is_skipped(tmp_path):
    sub = crud.create_submission("Jane Doe", "JD", 10, full_week_availability())
    crud.set_submission_status(sub.id, "imported")

    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as checkpointer:
        app = build_graph(checkpointer)
        result = app.invoke(
            {"submission_file_paths": [], "app_submission_ids": [sub.id]},
            config={"configurable": {"thread_id": "skip-test"}},
        )

    assert any("no longer pending" in e for e in result["ingestion_errors"])
    assert result["validation_errors"]  # nothing left to ingest, so the run stops
