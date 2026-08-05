"""
The orchestration graph: ingest -> validate -> solve -> (diagnose+explain
if infeasible, otherwise human review) -> output.

Run directly to see a full synthetic run, including the interrupt/resume
cycle at the human-review gate:
    python graph.py
"""

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from model_input import SolverInput
from router import ingest
from solve import solve as run_solver
from state import PipelineState


def ingest_node(state: PipelineState) -> dict:
    submissions = []
    errors = []
    for path in state["submission_file_paths"]:
        try:
            submissions.append(ingest(path))
        except Exception as e:
            errors.append(f"{path}: {e}")

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


def _join_roster_and_availability(state: PipelineState) -> SolverInput:
    """TEMPORARY name-matching stub - NOT the real name-linking design.

    The notes doc explicitly defers the roster<->availability linking
    decision (manual vs. auto-match). This is a placeholder exact-name
    match ONLY so the graph is testable end-to-end - it must be replaced
    once that decision is actually made. See PHASE4_HANDOFF.md.
    """
    by_name = {s.name: s for s in state["availability_submissions"]}
    for person in state["roster"]:
        sub = by_name.get(person.name)
        if sub is not None:
            person.availability = sub.availability
    return SolverInput(people=state["roster"])


def solve_node(state: PipelineState) -> dict:
    if state["validation_errors"]:
        return {"solve_status": "SKIPPED", "infeasibility_gaps": state["validation_errors"]}

    data = _join_roster_and_availability(state)

    # solve() already runs diagnose_coverage_gaps() internally and short-circuits
    # before the CP-SAT build if there's an obvious headcount gap - no need to
    # duplicate that check here. Its result carries the specific gaps either way.
    result = run_solver(data, time_limit_seconds=30)
    gaps = [] if result["feasible"] else (result["coverage_gaps"] or ["Solver could not find a feasible schedule."])
    return {
        "solve_status": result["status"],
        "solve_result": result if result["feasible"] else None,
        "infeasibility_gaps": gaps,
    }


def explain_node(state: PipelineState) -> dict:
    # Deterministic/template explanation for now. The notes doc (Section
    # 3) flags explain/repair as legitimate LLM territory - not wired up
    # yet, this is a plain formatted summary. See PHASE4_HANDOFF.md.
    lines = ["Could not build a valid schedule. Gaps found:"]
    lines.extend(f"  - {gap}" for gap in state["infeasibility_gaps"])
    return {"final_schedule": None, "review_notes": "\n".join(lines)}


def human_review_node(state: PipelineState) -> Command:
    decision = interrupt(
        {
            "message": "Schedule ready for review.",
            "solve_status": state["solve_status"],
            "hours_assigned": state["solve_result"]["hours_assigned"] if state["solve_result"] else {},
        }
    )
    return Command(update={"review_decision": decision.get("decision", "rejected")})


def output_node(state: PipelineState) -> dict:
    if state["review_decision"] == "approved":
        return {"final_schedule": state["solve_result"]}
    return {"final_schedule": None, "review_notes": "Schedule was not approved."}


def _route_after_solve(state: PipelineState) -> str:
    return "explain" if state["infeasibility_gaps"] else "human_review"


def _route_after_review(state: PipelineState) -> str:
    return "output" if state["review_decision"] == "approved" else "end"


def build_graph(checkpointer):
    graph = StateGraph(PipelineState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("validate", validate_node)
    graph.add_node("solve", solve_node)
    graph.add_node("explain", explain_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("output", output_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "validate")
    graph.add_edge("validate", "solve")
    graph.add_conditional_edges("solve", _route_after_solve, {"explain": "explain", "human_review": "human_review"})
    graph.add_edge("explain", END)
    graph.add_conditional_edges("human_review", _route_after_review, {"output": "output", "end": END})
    graph.add_edge("output", END)

    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    from model_input import Person

    def full_week_availability():
        weekday = [True] * 24 + [False]
        fri = [True] * 18 + [False] * 7
        weekend = [False] * 8 + [True] * 10 + [False] * 7
        return {"Mon": weekday, "Tue": weekday, "Wed": weekday, "Thu": weekday,
                "Fri": fri, "Sat": weekend, "Sun": weekend}

    roster = [Person(i, f"Tech{i}", "tech_only", 3, 1, 15, {}) for i in range(1, 11)]
    roster += [Person(i, f"Asst{i}", "assistant_only", 3, 1, 15, {}) for i in range(11, 21)]

    # Build tiny synthetic xlsx files so ingest_node has something real to read.
    # Built from a blank workbook rather than the real Blank_Schedule.xlsx
    # template - parse_xlsx() only reads specific cells (see xlsx_parser.py's
    # DAY_COLUMNS/NAME_CELL/HOURS_CELL), so a bare sheet with those cells set
    # is sufficient and keeps this self-test runnable without the template file.
    import openpyxl
    import tempfile

    tmpdir = tempfile.mkdtemp()
    paths = []
    for p in roster:
        path = f"{tmpdir}/{p.name}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["C2"] = p.name
        ws["J2"] = p.hours_requested
        avail = full_week_availability()
        day_cols = {"Mon": "C", "Tue": "E", "Wed": "G", "Thu": "I", "Fri": "K", "Sat": "M", "Sun": "O"}
        for day, col in day_cols.items():
            for i, is_avail in enumerate(avail[day]):
                if is_avail:
                    ws[f"{col}{6+i}"] = "X"
        wb.save(path)
        paths.append(path)

    with SqliteSaver.from_conn_string(f"{tmpdir}/checkpoints.db") as checkpointer:
        app = build_graph(checkpointer)
        config = {"configurable": {"thread_id": "test-run-1"}}

        result = app.invoke(
            {"submission_file_paths": paths, "roster": roster},
            config=config,
        )
        print("Paused at:", list(result.get("__interrupt__", "no interrupt"))[:1] or result)

        # Simulate the boss approving, potentially after a "restart"
        # (new checkpointer connection, same thread_id)
        final = app.invoke(Command(resume={"decision": "approved"}), config=config)
        print("Final solve_status:", final["solve_status"])
        print("Final schedule present:", final["final_schedule"] is not None)
        if final["final_schedule"]:
            print("Sample hours_assigned:", dict(list(final["final_schedule"]["hours_assigned"].items())[:3]))
