"""
The pipeline runner (scheduler/pipeline/runner.py): starting, pausing and
resuming the one in-flight run, and what its checkpoints keep.

Covers:
  (a) a new run starts clean instead of inheriting the last run's locks
      and schedule (regression: both used to carry over), while the last
      approved schedule stays saved
  (b) resume() refuses when the run isn't paused
  (c) a paused run survives a process restart
  (d) every class stored in pipeline state is on the checkpoint
      serializer's allowlist
  (e) the Postgres checkpointer, against a real database, including
      row-level security on its tables - skipped unless TEST_POSTGRES_URL
      is set
"""

import os
import typing

import pytest
from helpers import no_availability

from scheduler.db import crud, database
from scheduler.ingest.schema import AvailabilitySubmission
from scheduler.pipeline import runner
from scheduler.pipeline.graph import REVIEW, ROSTER_CONFIRM
from scheduler.pipeline.state import PipelineState
from scheduler.solver.model_input import LockedAssignment

TECHS = ("Tech1", "Tech2")


def restart_runner():
    """Drop the cached graph and close its checkpoint connection, as a
    process restart would."""
    if runner.graph_app.cache_info().currsize:
        runner.graph_app().checkpointer.conn.close()
    runner.graph_app.cache_clear()


@pytest.fixture(autouse=True)
def fresh_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    restart_runner()
    yield
    restart_runner()


def weekend_availability() -> dict[str, list[bool]]:
    """Sat and Sun 12:00-17:00 only - the hard weekend-coverage window. Two
    techs available just then make a small, quick, feasible solve."""
    availability = no_availability()
    for day in ("Sat", "Sun"):
        availability[day] = [8 <= slot < 18 for slot in range(len(availability[day]))]
    return availability


def seed_roster() -> dict[str, int]:
    """Complete roster rows, so roster_confirm finds an exact match for
    each submission and the completeness gate passes."""
    return {name: crud.add_person(name, name[0] + name[-1], "tech_only", 3, 1, 10).id for name in TECHS}


def submit_all() -> list[int]:
    return [crud.create_submission(name, "XX", 10, weekend_availability()).id for name in TECHS]


def confirm_all_matches():
    _, payload = runner.current_state()
    decisions = [{"index": c["index"], "action": "confirm"} for c in payload["candidates"]]
    return runner.resume({"decisions": decisions})


def test_new_run_does_not_inherit_the_previous_runs_state():
    ids = seed_roster()

    # Run 1: confirm, apply one lock, approve.
    runner.start_run([], submit_all())
    confirm_all_matches()
    lock = {"person_id": ids["Tech1"], "day": "Sat", "slot": 10, "role": "tech", "value": True}
    result = runner.resume({"decision": "edit", "add_locks": [lock]})
    assert not (result.get("lock_conflicts") or result.get("infeasibility_gaps"))
    values, _ = runner.current_state()
    assert values["locked_assignments"] == [LockedAssignment(**lock)]
    assert runner.resume({"decision": "approved"})["final_schedule"] is not None
    approved = crud.latest_schedule()

    # Run 2, paused at roster_confirm: nothing from run 1 is left.
    runner.start_run([], submit_all())
    values, payload = runner.current_state()
    assert payload["kind"] == ROSTER_CONFIRM
    for key in ("locked_assignments", "solve_result", "final_schedule", "review_decision", "schedule_id"):
        assert values.get(key) is None, f"{key} carried over from the previous run"

    # And run 2's first solve ran without run 1's lock.
    confirm_all_matches()
    values, payload = runner.current_state()
    assert payload["kind"] == REVIEW
    assert payload["locks"] == []

    # Run 1's approved schedule is still the saved one.
    assert crud.latest_schedule().id == approved.id


def test_resume_refuses_when_nothing_is_paused():
    with pytest.raises(runner.NoPausedRun):
        runner.resume({})

    # A run with nothing to ingest fails validation and ends without pausing.
    result = runner.start_run([], [])
    assert "__interrupt__" not in result
    with pytest.raises(runner.NoPausedRun):
        runner.resume({})


def test_a_paused_run_survives_a_restart():
    seed_roster()
    runner.start_run([], submit_all())

    restart_runner()

    values, payload = runner.current_state()
    assert payload["kind"] == ROSTER_CONFIRM
    assert all(isinstance(s, AvailabilitySubmission) for s in values["availability_submissions"])
    assert "__interrupt__" in confirm_all_matches()


def _scheduler_classes(hint) -> set[type]:
    """Classes from this project referenced anywhere in a type hint,
    e.g. list[LockedAssignment] -> {LockedAssignment}."""
    found = set()
    if isinstance(hint, type) and hint.__module__.startswith("scheduler."):
        found.add(hint)
    for arg in typing.get_args(hint):
        found |= _scheduler_classes(arg)
    return found


def test_every_class_in_pipeline_state_is_allowlisted():
    in_state = set().union(*(_scheduler_classes(h) for h in typing.get_type_hints(PipelineState).values()))
    missing = in_state - set(runner.CHECKPOINT_TYPES)
    assert not missing, f"add these to runner.CHECKPOINT_TYPES: {sorted(c.__name__ for c in missing)}"


def test_allowlisted_classes_round_trip(caplog):
    serde = runner.checkpoint_serializer()
    samples = [
        AvailabilitySubmission(name="Jane Doe", hours_requested=10, availability=weekend_availability()),
        LockedAssignment(person_id=1, day="Sat", slot=10, role="tech", value=True),
    ]
    for obj in samples:
        assert serde.loads_typed(serde.dumps_typed(obj)) == obj
    assert not [r for r in caplog.records if "msgpack" in r.getMessage()]


def test_libpq_url_drops_the_sqlalchemy_driver():
    assert runner._libpq_url("postgresql+psycopg2://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"
    assert runner._libpq_url("postgresql://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not set")
def test_postgres_checkpointer_keeps_a_paused_run(monkeypatch):
    # Create the roster's engine first, so the roster stays on the test
    # SQLite DB and only the checkpointer sees DATABASE_URL.
    database.get_engine()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    restart_runner()
    assert type(runner.graph_app().checkpointer).__name__ == "PostgresSaver"

    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_URL"]) as conn:
        rows = conn.execute(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(%s)",
            (list(runner.CHECKPOINT_TABLES),),
        ).fetchall()
    assert dict(rows) == dict.fromkeys(runner.CHECKPOINT_TABLES, True), "row-level security should be on"

    seed_roster()
    runner.start_run([], submit_all())
    restart_runner()

    values, payload = runner.current_state()
    assert payload["kind"] == ROSTER_CONFIRM
    assert all(isinstance(s, AvailabilitySubmission) for s in values["availability_submissions"])
    assert "__interrupt__" in confirm_all_matches()
