"""
Manual locks and the human-review edit/re-solve loop.

Covers:
  (a) a valid lock actually holds in a solved schedule, in both the
      force-in and force-out directions
  (b) an invalid lock (e.g. an assistant-only person locked into a tech
      role) is caught by validate_locks() before it reaches build_model()
  (c) the full pause -> edit -> re-solve -> pause -> approve cycle through
      the LangGraph pipeline, including falling back to the last good
      schedule when an edit makes the week infeasible
"""

import openpyxl
from helpers import full_week_availability
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from scheduler.db import crud
from scheduler.pipeline.graph import build_graph
from scheduler.solver.locks import validate_locks
from scheduler.solver.model_input import LockedAssignment, Person, SolverInput
from scheduler.solver.solve import solve as run_solver

DAY_COLUMNS = {"Mon": "C", "Tue": "E", "Wed": "G", "Thu": "I", "Fri": "K", "Sat": "M", "Sun": "O"}


def make_roster() -> list[Person]:
    roster = [Person(i, f"Tech{i}", "tech_only", 3, 1, 15, full_week_availability()) for i in range(1, 11)]
    roster += [Person(i, f"Asst{i}", "assistant_only", 3, 1, 15, full_week_availability()) for i in range(11, 21)]
    return roster


def write_submission_files(roster: list[Person], tmp_path) -> list[str]:
    """Minimal template-shaped xlsx files - parse_xlsx() only reads fixed cells."""
    paths = []
    for p in roster:
        path = tmp_path / f"{p.name}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["C2"] = p.name
        ws["J2"] = p.hours_requested
        for day, col in DAY_COLUMNS.items():
            for i, is_avail in enumerate(p.availability[day]):
                if is_avail:
                    ws[f"{col}{6 + i}"] = "X"
        wb.save(path)
        paths.append(str(path))
    return paths


def test_valid_locks_hold():
    roster = make_roster()
    data = SolverInput(people=roster)
    baseline = run_solver(data, time_limit_seconds=20)
    assert baseline["feasible"], f"baseline expected feasible, got {baseline['status']}"

    # Force-out: take a real baseline assignment and lock that person OUT of it.
    victim = baseline["assignments"][0]
    force_out = LockedAssignment(
        person_id=next(p.id for p in roster if p.name == victim["person"]),
        day=victim["day"], slot=victim["slot"], role=victim["role"], value=False,
    )
    # Force-in: a tech-capable person into a slot they're available for.
    tech2 = next(p for p in roster if p.name == "Tech2")
    force_in = LockedAssignment(person_id=tech2.id, day="Wed", slot=10, role="tech", value=True)

    data.locked_assignments = [force_out, force_in]
    assert validate_locks(data) == []

    result = run_solver(data, time_limit_seconds=20)
    assert result["feasible"], f"expected feasible with locks applied, got {result['status']}"

    assert not any(
        a["person"] == victim["person"] and a["day"] == victim["day"]
        and a["slot"] == victim["slot"] and a["role"] == victim["role"]
        for a in result["assignments"]
    ), "force-out lock did not hold"
    assert any(
        a["person"] == "Tech2" and a["day"] == "Wed" and a["slot"] == 10 and a["role"] == "tech"
        for a in result["assignments"]
    ), "force-in lock did not hold"


def test_invalid_lock_caught_before_build_model():
    roster = make_roster()
    assistant = next(p for p in roster if p.role_weighting == "assistant_only")
    bad_lock = LockedAssignment(person_id=assistant.id, day="Mon", slot=5, role="tech", value=True)

    data = SolverInput(people=roster, locked_assignments=[bad_lock])
    conflicts = validate_locks(data)
    assert conflicts, "expected a capability conflict for an assistant-only person locked into tech"
    assert "tech" in conflicts[0].lower()

    # Without validation, the solver must not quietly honor the lock either.
    assert not run_solver(data, time_limit_seconds=5)["feasible"]


def test_full_edit_resolve_cycle(tmp_path):
    roster = make_roster()
    paths = write_submission_files(roster, tmp_path)

    # The graph builds its solver input from the DB roster, so seed complete
    # rows - roster_completeness_check then passes with nothing to fix.
    db_id_by_name = {
        p.name: crud.add_person(
            p.name, f"I{p.id}", p.role_weighting, p.experience_rating, p.proximity, p.hours_requested
        ).id
        for p in roster
    }

    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as checkpointer:
        app = build_graph(checkpointer)
        config = {"configurable": {"thread_id": "edit-loop-test"}}

        result = app.invoke({"submission_file_paths": paths}, config=config)
        payload = result["__interrupt__"][0].value
        assert "candidates" in payload, "expected the first pause to be roster_confirm"

        decisions = [{"index": c["index"], "action": "confirm"} for c in payload["candidates"]]
        result = app.invoke(Command(resume={"decisions": decisions}), config=config)
        assert "__interrupt__" in result, "expected a pause at human_review"
        assert "candidates" not in result["__interrupt__"][0].value

        # A valid edit loops back to solve and the lock shows up in the schedule.
        lock = {"person_id": db_id_by_name["Tech2"], "day": "Thu", "slot": 12, "role": "tech", "value": True}
        result = app.invoke(Command(resume={"decision": "edit", "locked_assignments": [lock]}), config=config)
        assert not result.get("lock_conflicts")
        assert "__interrupt__" in result
        assert any(
            a["person"] == "Tech2" and a["day"] == "Thu" and a["slot"] == 12 and a["role"] == "tech"
            for a in result["solve_result"]["assignments"]
        )
        good_solve_result = result["solve_result"]

        # Force every tech OUT of one weekend slot. Each lock is individually
        # valid, so validate_locks() passes, but weekend coverage (hard) becomes
        # unsatisfiable. The pipeline must fall back to the last good schedule
        # and pause at human_review again rather than dead-ending at explain.
        breaking_locks = [lock] + [
            {"person_id": db_id_by_name[f"Tech{i}"], "day": "Sat", "slot": 10, "role": "tech", "value": False}
            for i in range(1, 11)
        ]
        result = app.invoke(Command(resume={"decision": "edit", "locked_assignments": breaking_locks}), config=config)
        assert not result.get("lock_conflicts")
        assert result.get("infeasibility_gaps")
        assert "__interrupt__" in result
        assert result["solve_result"] == good_solve_result

        # Recover with the good lock alone, then approve.
        result = app.invoke(Command(resume={"decision": "edit", "locked_assignments": [lock]}), config=config)
        assert not result.get("infeasibility_gaps") and not result.get("lock_conflicts")
        assert "__interrupt__" in result

        final = app.invoke(Command(resume={"decision": "approved"}), config=config)
        assert final["final_schedule"] is not None
