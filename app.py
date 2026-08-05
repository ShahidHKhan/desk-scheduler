"""
Streamlit review UI for the orchestration graph (graph.py).

Run with:
    streamlit run app.py
"""

import sqlite3
import tempfile
from pathlib import Path

import openpyxl
import streamlit as st
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import crud
from crud import RosterValidationError
from format_output import build_schedule_grid
from graph import build_graph
from model_input import DAYS, ROLES, Person
from models import ROLE_WEIGHTINGS

CHECKPOINT_DB = "graph_checkpoints.db"
# Single-user tool: one in-flight pipeline run at a time is an accepted
# simplification, not an oversight - a fixed thread_id means every
# invoke/resume in this app talks to the same run.
THREAD_ID = "main"


@st.cache_resource
def get_graph_app():
    # Raw SqliteSaver(conn), NOT `with SqliteSaver.from_conn_string(...) as saver:`.
    # The context-manager form closes its connection when the `with` block
    # exits, which happens almost immediately inside a cached-resource
    # function - that breaks checkpoint persistence across Streamlit reruns.
    # Confirmed by testing: building a saver this way, compiling two separate
    # graph objects against the same connection (simulating two reruns), and
    # checking state persists across them under the same thread_id.
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return build_graph(checkpointer)


def _config():
    return {"configurable": {"thread_id": THREAD_ID}}


def render_roster_tab():
    st.subheader("Roster")
    roster = crud.list_roster()

    for person in roster:
        cols = st.columns([3, 1, 2, 1, 1, 1, 1])
        cols[0].write(person.name)
        cols[1].write(person.initials)
        cols[2].write(person.role_weighting)
        cols[3].write(person.experience_rating)
        cols[4].write(person.proximity)
        cols[5].write(person.hours_requested)
        if cols[6].button("Delete", key=f"delete_{person.id}"):
            crud.delete_person(person.id)
            st.rerun()

    st.markdown("### Add person")
    with st.form("add_person_form", clear_on_submit=True):
        name = st.text_input("Name")
        initials = st.text_input("Initials")
        role_weighting = st.selectbox("Role weighting", ROLE_WEIGHTINGS)
        experience_rating = st.slider("Experience rating", 1, 4, 2)
        proximity = st.slider("Proximity", 1, 3, 2)
        hours_requested = st.number_input("Hours requested", min_value=3, max_value=20, value=10, step=1)
        submitted = st.form_submit_button("Add person")

        if submitted:
            try:
                crud.add_person(
                    name, initials, role_weighting, experience_rating, proximity, int(hours_requested)
                )
            except RosterValidationError as e:
                st.error(str(e))
            else:
                st.rerun()


def render_run_pipeline_tab():
    st.subheader("Run Pipeline")
    roster = crud.list_roster()
    if not roster:
        st.info("Add people to the roster first (see the Roster tab).")
        return

    roster_names = [p.name for p in roster]
    uploaded_files = st.file_uploader(
        "Availability submissions", type=["xlsx", "pdf"], accept_multiple_files=True
    )

    file_owners: dict[str, str] = {}
    for f in uploaded_files or []:
        file_owners[f.name] = st.selectbox(f"Who submitted {f.name}?", roster_names, key=f"owner_{f.name}")

    if st.button("Run pipeline", disabled=not uploaded_files):
        tmpdir = tempfile.mkdtemp()
        paths = []
        for f in uploaded_files:
            path = str(Path(tmpdir) / f.name)
            with open(path, "wb") as out:
                out.write(f.getbuffer())

            # Roster<->availability linking is fully manual, no fuzzy matching
            # (confirmed decision - PHASE2/3/4 handoffs). The selectbox choice
            # above is authoritative, so for .xlsx we overwrite the name cell
            # to match it exactly, guaranteeing _join_roster_and_availability's
            # exact-name match links to the right person regardless of what the
            # submitter actually typed in the file. PDF text can't be rewritten
            # this way, so PDF submissions still rely on whatever name the
            # parser read out of the file.
            if path.lower().endswith(".xlsx"):
                wb = openpyxl.load_workbook(path)
                wb.active["C2"] = file_owners[f.name]
                wb.save(path)

            paths.append(path)

        people = [
            Person(
                p.id, p.name, p.role_weighting, p.experience_rating,
                p.proximity, p.hours_requested, {}, p.initials,
            )
            for p in roster
        ]

        result = get_graph_app().invoke(
            {"submission_file_paths": paths, "roster": people}, config=_config()
        )
        st.session_state["last_result"] = result
        st.session_state["people_lookup"] = {p.name: p for p in people}
        st.session_state["locks"] = []

        gaps = result.get("infeasibility_gaps") or []
        conflicts = result.get("lock_conflicts") or []
        if gaps or conflicts:
            for g in gaps:
                st.error(g)
            for c in conflicts:
                st.error(c)
        else:
            st.success("Pipeline ran - see the Review & Edit tab.")


def render_review_tab():
    st.subheader("Review & Edit")
    result = st.session_state.get("last_result")
    if not result or not result.get("solve_result"):
        st.info("Run the pipeline first (see the Run Pipeline tab).")
        return

    people_lookup = st.session_state.get("people_lookup", {})
    grid = build_schedule_grid(result["solve_result"], people_lookup)
    st.dataframe(grid, use_container_width=True)
    st.write("Hours assigned:", result["solve_result"]["hours_assigned"])

    col1, col2 = st.columns(2)
    if col1.button("Approve"):
        _resume_review({"decision": "approved"})
    if col2.button("Reject"):
        _resume_review({"decision": "rejected"})

    st.markdown("### Edit a slot")
    with st.form("lock_form"):
        person_name = st.selectbox("Person", list(people_lookup.keys()))
        day = st.selectbox("Day", DAYS)
        slot = st.number_input(
            "Slot index (0 = 08:00, one per half hour)", min_value=0, max_value=24, value=0, step=1
        )
        role = st.selectbox("Role", ROLES)
        force = st.radio("Force", ["In", "Out"], horizontal=True)
        submitted = st.form_submit_button("Apply edit")

        if submitted:
            person = people_lookup[person_name]
            new_lock = {
                "person_id": person.id,
                "day": day,
                "slot": int(slot),
                "role": role,
                "value": force == "In",
            }
            # human_review_node replaces locked_assignments wholesale on each
            # "edit" resume, so re-send every lock applied so far this
            # session, not just the newest one - otherwise earlier edits get
            # silently dropped by the next one.
            locks = st.session_state.get("locks", []) + [new_lock]
            new_result = _resume_review({"decision": "edit", "locked_assignments": locks}, rerun=False)
            if new_result is not None:
                # A re-solve can fail two ways: the lock itself is
                # contradictory (lock_conflicts), or it's individually valid
                # but breaks coverage elsewhere (infeasibility_gaps) - either
                # way the edit didn't take, so don't commit it into the
                # accumulated locks list or every future resend would keep
                # re-applying a lock that's known to break the schedule.
                gaps = new_result.get("infeasibility_gaps") or []
                conflicts = new_result.get("lock_conflicts") or []
                if gaps or conflicts:
                    for c in conflicts:
                        st.error(c)
                    for g in gaps:
                        st.error(g)
                    st.warning("Edit not applied - still showing your last approved-pending schedule.")
                else:
                    st.session_state["locks"] = locks
                    st.rerun()


def _resume_review(resume_payload: dict, rerun: bool = True):
    """Resume the paused graph at human_review. Returns the new result, or
    None if there was nothing left to resume (e.g. a prior lock conflict
    already routed the graph to explain -> END, closing out that thread)."""
    try:
        new_result = get_graph_app().invoke(Command(resume=resume_payload), config=_config())
    except Exception as e:
        st.error(
            f"Could not resume review ({e}). The run may have already ended - "
            f"go back to Run Pipeline to start a new one."
        )
        return None
    st.session_state["last_result"] = new_result
    if rerun:
        st.rerun()
    return new_result


def render_output_tab():
    st.subheader("Output")
    result = st.session_state.get("last_result")
    if not result or not result.get("final_schedule"):
        st.info("No approved schedule yet - approve one in the Review & Edit tab first.")
        return

    people_lookup = st.session_state.get("people_lookup", {})
    grid = build_schedule_grid(result["final_schedule"], people_lookup)
    st.dataframe(grid, use_container_width=True)
    st.download_button(
        "Download CSV", grid.to_csv().encode("utf-8"), file_name="schedule.csv", mime="text/csv"
    )


st.set_page_config(page_title="Service Desk Scheduler", layout="wide")
st.title("Service Desk Scheduler")

tab_roster, tab_run, tab_review, tab_output = st.tabs(["Roster", "Run Pipeline", "Review & Edit", "Output"])
with tab_roster:
    render_roster_tab()
with tab_run:
    render_run_pipeline_tab()
with tab_review:
    render_review_tab()
with tab_output:
    render_output_tab()
