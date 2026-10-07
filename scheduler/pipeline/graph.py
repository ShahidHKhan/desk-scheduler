"""
The orchestration graph: ingest -> validate -> roster_confirm ->
roster_completeness_check -> solve -> (diagnose+explain if infeasible,
otherwise human review) -> output. From human review, an "add" decision
goes back to ingest with more submissions; the run's edits carry over.

Run directly to see a full synthetic run, including the interrupt/resume
cycles at the roster-confirm, roster-completeness, and human-review gates:
    python -m scheduler.pipeline.graph
"""

from dataclasses import asdict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from scheduler.db import crud
from scheduler.ingest.in_app import to_availability_submission
from scheduler.ingest.roster_match import match_submissions_to_roster
from scheduler.ingest.router import ingest
from scheduler.ingest.schema import hours_range_warning
from scheduler.pipeline.state import PipelineState
from scheduler.solver.locks import validate_locks
from scheduler.solver.model_input import LockedAssignment, Person, SolverInput
from scheduler.solver.solve import solve as run_solver

# Every interrupt payload carries one of these as "kind", so a UI can tell
# which pause the run is at without inspecting the rest of the payload.
ROSTER_CONFIRM = "roster_confirm"
ROSTER_INCOMPLETE = "roster_incomplete"
REVIEW = "review"


def ingest_node(state: PipelineState) -> dict:
    submissions = []
    errors = []
    for path in state.get("submission_file_paths") or []:
        try:
            submissions.append(ingest(path))
        except Exception as e:
            errors.append(f"{path}: {e}")

    # In-app forms are already structured - nothing to parse, just load them.
    for submission_id in state.get("app_submission_ids") or []:
        record = crud.get_submission(submission_id)
        if record is None or record.status != "pending":
            errors.append(f"In-app submission #{submission_id} is no longer pending - skipped.")
            continue
        submissions.append(to_availability_submission(record))

    warnings = [w for s in submissions for w in s.warnings]
    return {
        "availability_submissions": submissions,
        "ingestion_errors": errors,
        "ingestion_warnings": warnings,
    }


def validate_node(state: PipelineState) -> dict:
    errors = list(state.get("ingestion_errors", []))
    if not state["availability_submissions"]:
        errors.append("No availability submissions were successfully ingested.")
    return {"validation_errors": errors}


def roster_confirm_node(state: PipelineState) -> dict:
    """Human-in-the-loop gate: confirm each parsed submission against the
    roster before anything gets written.

    Nothing auto-commits. For each parsed submission we suggest an
    existing-roster match (or None); the boss confirms, redirects to a
    different person, or creates a new row. Only then do we write, via
    crud.upsert_from_submission() - which never touches a matched row's
    role_weighting/experience_rating/proximity/initials.
    """
    if state.get("validation_errors"):
        # Nothing was successfully ingested this run - solve_node's own
        # validation_errors check will short-circuit to SKIPPED, so there's
        # nothing meaningful to confirm.
        return {"submission_roster_ids": {}}

    submissions = state["availability_submissions"]
    candidates = match_submissions_to_roster(submissions, crud.list_roster())

    decision = interrupt(
        {
            "kind": ROSTER_CONFIRM,
            "message": "Confirm each parsed submission against the roster before committing.",
            "candidates": [
                {
                    "index": i,
                    "parsed_name": c.submission.name,
                    "hours_requested": c.submission.hours_requested,
                    "suggested_match_id": c.suggested_match_id,
                    "suggested_match_name": c.suggested_match_name,
                    "source": c.submission.source_file,
                    "from_app": c.submission.parser_used == "in_app",
                }
                for i, c in enumerate(candidates)
            ],
        }
    )

    submission_roster_ids: dict[int, int] = {}
    for raw in decision.get("decisions", []):
        idx = raw["index"]
        action = raw.get("action", "new")
        if action == "new":
            person_id = None
        elif action == "choose_other":
            person_id = raw["person_id"]
        else:  # "confirm" - use the suggested match
            person_id = candidates[idx].suggested_match_id

        submission = candidates[idx].submission
        # Hours the roster can't hold (say, 25) are left for the boss to
        # set: written as-is they'd fail the roster's CHECK constraint.
        # Unset, the completeness check below asks for them instead.
        hours = submission.hours_requested if hours_range_warning(submission.hours_requested) is None else None
        person = crud.upsert_from_submission(
            person_id,
            submission.name,
            hours,
            submission.availability,
            initials=submission.initials,
        )
        submission_roster_ids[idx] = person.id
        if submission.app_submission_id is not None:
            crud.set_submission_status(submission.app_submission_id, "imported", roster_id=person.id)

    return {"submission_roster_ids": submission_roster_ids}


def roster_completeness_check_node(state: PipelineState) -> dict:
    """Hard gate: refuse to proceed to solve while any roster row is
    missing role_weighting, experience_rating, proximity, or initials
    Loops - re-checking after each resume - rather
    than a single pass, since the boss may need several trips to the
    Roster panel to clear every incomplete row.
    """
    if state.get("validation_errors"):
        return {"roster_incomplete_rows": []}

    while True:
        incomplete = crud.list_incomplete_roster()
        if not incomplete:
            return {"roster_incomplete_rows": []}

        interrupt(
            {
                "kind": ROSTER_INCOMPLETE,
                "message": (
                    "Solve is blocked: some roster rows are missing required "
                    "fields. Fill them in on the Roster panel, then continue."
                ),
                "incomplete_rows": [
                    {"id": p.id, "name": p.name, "missing_fields": p.missing_fields}
                    for p in incomplete
                ],
            }
        )
        # Resumed - loop back around and re-query; if still incomplete,
        # interrupt() fires again (a fresh call site within this same
        # while loop) rather than proceeding.


def _build_solver_input(state: PipelineState) -> SolverInput:
    """Build the solver's People list from the current (post-confirm,
    post-completeness-gate) roster in the DB, joined with this run's
    parsed availability via the person ids roster_confirm_node committed.

    Deliberately re-reads the roster from the DB on every call rather than
    threading it through state, so a re-solve after an edit (human_review
    -> solve loop) always reflects the committed roster, not a stale copy.
    """
    roster_rows = crud.list_roster()
    submissions = state.get("availability_submissions", [])
    submission_roster_ids = state.get("submission_roster_ids") or {}

    # This run's freshly-parsed availability, keyed by roster id - takes
    # priority since it's the most current data for whoever's file was part
    # of this run. Everyone else falls back to row.availability, persisted
    # on their roster row by a PRIOR run's roster_confirm_node (see
    # crud.upsert_from_submission()) - NOT an empty dict. Defaulting to {}
    # here was a real bug: a run that only re-uploads one person's
    # corrected file would silently zero out every other roster member's
    # availability for that solve.
    availability_by_person_id = {
        submission_roster_ids[idx]: sub.availability
        for idx, sub in enumerate(submissions)
        if idx in submission_roster_ids
    }

    people = [
        Person(
            id=row.id,
            name=row.name,
            role_weighting=row.role_weighting,
            experience_rating=row.experience_rating,
            proximity=row.proximity,
            hours_requested=row.hours_requested,
            availability=availability_by_person_id.get(row.id, row.availability),
            initials=row.initials or "",
        )
        for row in roster_rows
    ]
    return SolverInput(people=people)


def solve_node(state: PipelineState) -> dict:
    if state["validation_errors"]:
        return {"solve_status": "SKIPPED", "infeasibility_gaps": state["validation_errors"], "lock_conflicts": []}

    data = _build_solver_input(state)
    # An edit's proposed locks if there are any, otherwise the accepted
    # ones (none on a run's first solve). Every return below clears
    # proposed_locks: it's consumed by this solve whether or not it works.
    proposed = state.get("proposed_locks")
    data.locked_assignments = proposed if proposed is not None else state.get("locked_assignments") or []

    # Catch a contradictory manual edit before it ever reaches coverage
    # diagnosis or the CP-SAT solve - a self-contradictory request shouldn't
    # burn solve time or get lumped in with a generic coverage gap.
    lock_conflicts = validate_locks(data)
    if lock_conflicts:
        return {
            "solve_status": "LOCK_CONFLICT",
            "lock_conflicts": lock_conflicts,
            "infeasibility_gaps": [],
            "proposed_locks": None,
        }

    # solve() already runs diagnose_coverage_gaps() internally and short-circuits
    # before the CP-SAT build if there's an obvious headcount gap - no need to
    # duplicate that check here. Its result carries the specific gaps either way.
    result = run_solver(data, time_limit_seconds=30)
    if result["feasible"]:
        return {
            "solve_status": result["status"],
            "solve_result": result,
            "locked_assignments": data.locked_assignments,
            "proposed_locks": None,
            "infeasibility_gaps": [],
            "lock_conflicts": [],
        }

    # Deliberately omit "solve_result" here rather than setting it to None:
    # LangGraph's shallow merge then leaves whatever solve_result was already
    # in state untouched. On a re-solve triggered by an edit, that's the last
    # known-good schedule - _route_after_solve uses its presence to decide
    # whether this failure has something to fall back to (loop back to
    # human_review) or is a first-ever failure with nothing to show (explain).
    # locked_assignments is left alone for the same reason: it stays the
    # locks that last-good schedule was solved with.
    return {
        "solve_status": result["status"],
        "infeasibility_gaps": result["coverage_gaps"] or ["Solver could not find a feasible schedule."],
        "lock_conflicts": [],
        "proposed_locks": None,
    }


def explain_node(state: PipelineState) -> dict:
    # Deterministic/template explanation for now. Explain/repair is a
    # reasonable place for an LLM later (graded by evals/judge.py), but
    # this is a plain formatted summary.
    #
    # Lock conflicts and coverage gaps are different failure modes (a bad
    # manual edit vs. not enough staff) - the message needs to say which
    # one actually happened, not lump them together.
    lock_conflicts = state.get("lock_conflicts")
    if lock_conflicts:
        lines = ["Your manual edits conflict with a hard rule:"]
        lines.extend(f"  - {c}" for c in lock_conflicts)
    else:
        lines = ["Could not build a valid schedule. Gaps found:"]
        lines.extend(f"  - {gap}" for gap in state["infeasibility_gaps"])
    return {"final_schedule": None, "review_notes": "\n".join(lines)}


def human_review_node(state: PipelineState) -> Command:
    gaps = state.get("infeasibility_gaps") or []
    conflicts = state.get("lock_conflicts") or []
    # Weekday coverage is a soft constraint (build_model.py) - a feasible
    # solve can still leave weekday slots understaffed relative to the 2+2
    # target. Distinct from `gaps` above (which only ever holds
    # guaranteed-infeasible weekend gaps): this is informational on an
    # otherwise-successful solve, not a routing failure.
    shortfalls = state["solve_result"].get("coverage_shortfalls", []) if state["solve_result"] else []

    # Reaching human_review with gaps/conflicts already set means this is a
    # re-solve that failed (see _route_after_solve) - solve_result here is
    # the last known-good schedule, not the result of this failed attempt.
    # Say so explicitly rather than presenting it as if nothing went wrong.
    if gaps or conflicts:
        message = (
            "Your last change could not be applied - still showing your last "
            "approved-pending schedule. Try a different change, or approve/reject "
            "this one as-is."
        )
    elif shortfalls:
        message = f"Schedule ready for review - {len(shortfalls)} weekday slot(s) are understaffed."
    else:
        message = "Schedule ready for review."

    decision = interrupt(
        {
            "kind": REVIEW,
            "message": message,
            "solve_status": state["solve_status"],
            "people": state["solve_result"]["people"] if state["solve_result"] else [],
            "locks": [asdict(lock) for lock in state.get("locked_assignments") or []],
            "infeasibility_gaps": gaps,
            "lock_conflicts": conflicts,
            "coverage_shortfalls": shortfalls,
        }
    )
    # Resume with {"decision": "approved"} or {"decision": "rejected"}, or
    # {"decision": "edit", "add_locks": [...], "remove_locks": [...]}, or
    # {"decision": "add", "file_paths": [...], "app_submission_ids": [...]}
    # for submissions left out of the run.
    review_decision = decision.get("decision", "rejected")
    # validation_errors is only set here by an "add" with a file that
    # couldn't be read. It's been shown by now, and left set it would
    # make solve_node skip every later re-solve.
    update = {"review_decision": review_decision, "validation_errors": []}
    if review_decision == "add":
        # Only the new submissions go through ingest and roster_confirm.
        # Everyone confirmed earlier in the run keeps the availability
        # saved on their roster row, which _build_solver_input falls back
        # to, and locked_assignments is left alone so the edits carry over.
        update["submission_file_paths"] = decision.get("file_paths", [])
        update["app_submission_ids"] = decision.get("app_submission_ids", [])
    elif review_decision == "edit":
        update["proposed_locks"] = _edit_locks(
            state.get("locked_assignments") or [],
            add=[LockedAssignment(**raw) for raw in decision.get("add_locks", [])],
            remove=decision.get("remove_locks", []),
        )
    return Command(update=update)


def _lock_key(person_id: int, day: str, slot: int, role: str) -> tuple:
    return (person_id, day, slot, role)


def _edit_locks(
    accepted: list[LockedAssignment], add: list[LockedAssignment], remove: list[dict]
) -> list[LockedAssignment]:
    """The accepted locks with an edit applied. `remove` entries name a
    lock by person_id/day/slot/role. A lock added for a person, day, slot
    and role that already has one replaces it, so flipping force-in to
    force-out is a single edit."""
    removed = {_lock_key(r["person_id"], r["day"], r["slot"], r["role"]) for r in remove}
    added = {_lock_key(lock.person_id, lock.day, lock.slot, lock.role): lock for lock in add}
    kept = [
        lock
        for lock in accepted
        if _lock_key(lock.person_id, lock.day, lock.slot, lock.role) not in removed | added.keys()
    ]
    return kept + list(added.values())


def output_node(state: PipelineState) -> dict:
    if state["review_decision"] == "approved":
        # Saved outside the pipeline's own state, which the next run
        # starts over - see db.models.Schedule.
        locks = [asdict(lock) for lock in state.get("locked_assignments") or []]
        schedule = crud.save_schedule(state["solve_result"], locks)
        return {"final_schedule": state["solve_result"], "schedule_id": schedule.id}
    return {"final_schedule": None, "review_notes": "Schedule was not approved."}


def _route_after_solve(state: PipelineState) -> str:
    if not (state["infeasibility_gaps"] or state.get("lock_conflicts")):
        return "human_review"
    # A failure is only terminal (-> explain -> END) when there's no prior
    # schedule to fall back to. A failed re-solve during the edit loop (bad
    # lock, or an edit that opens a coverage gap) still has solve_result from
    # the last successful solve sitting in state - route back to human_review
    # so the boss can see what went wrong and try again instead of the whole
    # pipeline dead-ending.
    return "human_review" if state.get("solve_result") is not None else "explain"


def _route_after_review(state: PipelineState) -> str:
    decision = state["review_decision"]
    if decision == "approved":
        return "output"
    if decision == "edit":
        return "solve"
    if decision == "add":
        return "ingest"
    return "end"


def build_graph(checkpointer):
    graph = StateGraph(PipelineState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("validate", validate_node)
    graph.add_node("roster_confirm", roster_confirm_node)
    graph.add_node("roster_completeness_check", roster_completeness_check_node)
    graph.add_node("solve", solve_node)
    graph.add_node("explain", explain_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("output", output_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "validate")
    graph.add_edge("validate", "roster_confirm")
    graph.add_edge("roster_confirm", "roster_completeness_check")
    graph.add_edge("roster_completeness_check", "solve")
    graph.add_conditional_edges("solve", _route_after_solve, {"explain": "explain", "human_review": "human_review"})
    graph.add_edge("explain", END)
    graph.add_conditional_edges(
        "human_review",
        _route_after_review,
        {"output": "output", "solve": "solve", "ingest": "ingest", "end": END},
    )
    graph.add_edge("output", END)

    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    from scheduler.db import database

    def full_week_availability():
        weekday = [True] * 24 + [False]
        fri = [True] * 18 + [False] * 7
        weekend = [False] * 8 + [True] * 10 + [False] * 7
        return {"Mon": weekday, "Tue": weekday, "Wed": weekday, "Thu": weekday,
                "Fri": fri, "Sat": weekend, "Sun": weekend}

    people_specs = [(f"Tech{i}", "tech_only", f"T{i}") for i in range(1, 11)]
    people_specs += [(f"Asst{i}", "assistant_only", f"A{i}") for i in range(11, 21)]

    # Seed a complete roster row per synthetic person first - roster_confirm
    # then finds an exact-name match for every submission, and
    # roster_completeness_check passes with nothing to fix, so this self-test
    # exercises the happy path straight through to human_review. Cleaned up
    # at the end so repeat runs don't pile up rows in the dev roster.db.
    database.init_db()
    inserted_ids = []
    for name, role_weighting, initials in people_specs:
        person = crud.add_person(name, initials, role_weighting, 3, 1, 15)
        inserted_ids.append(person.id)

    # Build tiny synthetic xlsx files so ingest_node has something real to read.
    # Built from a blank workbook rather than the real Blank_Schedule.xlsx
    # template - parse_xlsx() only reads specific cells (see xlsx_parser.py's
    # DAY_COLUMNS/NAME_CELL/HOURS_CELL), so a bare sheet with those cells set
    # is sufficient and keeps this self-test runnable without the template file.
    import tempfile

    import openpyxl

    tmpdir = tempfile.mkdtemp()
    paths = []
    for name, _role_weighting, _initials in people_specs:
        path = f"{tmpdir}/{name}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["C2"] = name
        ws["J2"] = 15
        avail = full_week_availability()
        day_cols = {"Mon": "C", "Tue": "E", "Wed": "G", "Thu": "I", "Fri": "K", "Sat": "M", "Sun": "O"}
        for day, col in day_cols.items():
            for i, is_avail in enumerate(avail[day]):
                if is_avail:
                    ws[f"{col}{6+i}"] = "X"
        wb.save(path)
        paths.append(path)

    try:
        with SqliteSaver.from_conn_string(f"{tmpdir}/checkpoints.db") as checkpointer:
            app = build_graph(checkpointer)
            config = {"configurable": {"thread_id": "test-run-1"}}

            result = app.invoke({"submission_file_paths": paths}, config=config)
            interrupt_payload = result["__interrupt__"][0].value
            print("Paused at roster_confirm:", interrupt_payload["message"])

            # Every submission's name exactly matches a roster row we just
            # seeded, so confirm every suggested match as-is.
            decisions = [{"index": c["index"], "action": "confirm"} for c in interrupt_payload["candidates"]]
            result = app.invoke(Command(resume={"decisions": decisions}), config=config)
            print("Paused at:", list(result.get("__interrupt__", "no interrupt"))[:1] or result)

            # Simulate the boss approving, potentially after a "restart"
            # (new checkpointer connection, same thread_id)
            final = app.invoke(Command(resume={"decision": "approved"}), config=config)
            print("Final solve_status:", final["solve_status"])
            print("Final schedule present:", final["final_schedule"] is not None)
            if final["final_schedule"]:
                sample = final["final_schedule"]["people"][:3]
                print("Sample hours assigned:", {p["name"]: p["hours_assigned"] for p in sample})
    finally:
        for person_id in inserted_ids:
            crud.delete_person(person_id)
