"""
Streamlit review UI for the orchestration graph (graph.py).

Run with:
    streamlit run app.py
"""

import sqlite3
import tempfile
from pathlib import Path

import streamlit as st
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import crud
from crud import RosterValidationError
from format_output import build_schedule_grid
from graph import build_graph
from model_input import DAYS, ROLES
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


def _current_interrupt(result: dict | None) -> dict | None:
    """Return the payload of the graph's current interrupt(), or None if
    the run isn't paused (finished, or hasn't started)."""
    if not result:
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    return interrupts[0].value


def _resume_graph(resume_payload: dict, rerun: bool = True):
    """Resume the paused graph (at whichever node is currently interrupted).
    Returns the new result, or None if there was nothing left to resume
    (e.g. a prior lock conflict already routed the graph to explain -> END,
    closing out that thread)."""
    try:
        new_result = get_graph_app().invoke(Command(resume=resume_payload), config=_config())
    except Exception as e:
        st.error(
            f"Could not resume ({e}). The run may have already ended - "
            f"go back to Import Availability to start a new one."
        )
        return None
    st.session_state["last_result"] = new_result
    if rerun:
        st.rerun()
    return new_result


def render_roster_tab():
    st.subheader("Roster")
    roster = crud.list_roster()
    incomplete_ids = {p.id for p in roster if not p.is_complete}

    if incomplete_ids:
        st.warning(
            f"{len(incomplete_ids)} roster row(s) are missing required fields - "
            f"solve is blocked until they're completed."
        )

    show_incomplete_only = st.checkbox("Show incomplete only", value=False)
    visible_roster = [p for p in roster if p.id in incomplete_ids] if show_incomplete_only else roster

    for person in visible_roster:
        incomplete = person.id in incomplete_ids
        label = f"{person.name} - INCOMPLETE" if incomplete else person.name
        with st.expander(label, expanded=incomplete):
            with st.form(f"edit_{person.id}"):
                initials = st.text_input("Initials", value=person.initials or "")
                role_weighting_options = ["(unset)"] + list(ROLE_WEIGHTINGS)
                current_role_weighting = person.role_weighting if person.role_weighting in ROLE_WEIGHTINGS else "(unset)"
                role_weighting = st.selectbox(
                    "Role weighting", role_weighting_options,
                    index=role_weighting_options.index(current_role_weighting),
                )
                experience_rating = st.slider(
                    "Experience rating", 1, 4, person.experience_rating if person.experience_rating else 2
                )
                proximity = st.slider("Proximity", 1, 3, person.proximity if person.proximity else 2)
                hours_requested = st.number_input(
                    "Hours requested", min_value=3, max_value=20,
                    value=person.hours_requested if person.hours_requested else 10, step=1,
                )
                col1, col2 = st.columns(2)
                save = col1.form_submit_button("Save")
                delete = col2.form_submit_button("Delete")

            if save:
                if role_weighting == "(unset)":
                    st.error("Role weighting is one of the four required fields - it can't be left unset.")
                else:
                    try:
                        crud.update_person(
                            person.id,
                            initials=initials or None,
                            role_weighting=role_weighting,
                            experience_rating=int(experience_rating),
                            proximity=int(proximity),
                            hours_requested=int(hours_requested),
                        )
                    except RosterValidationError as e:
                        st.error(str(e))
                    else:
                        st.rerun()
            if delete:
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


def _render_roster_confirm(payload: dict):
    """Confirm screen for roster_confirm_node's interrupt: one row per
    parsed submission, "matches [name]" / "new person", with a control to
    confirm, redirect to a different person, or add as new. Nothing is
    written to the roster until this form is submitted."""
    st.info(payload["message"])
    roster = crud.list_roster()
    roster_names = [p.name for p in roster]
    roster_id_by_name = {p.name: p.id for p in roster}

    with st.form("roster_confirm_form"):
        rows = []
        for c in payload["candidates"]:
            st.markdown(f"**{c['parsed_name']}** - {c['hours_requested']} hrs requested")
            if c["suggested_match_id"] is not None:
                st.caption(f"Matches existing roster entry: {c['suggested_match_name']}")
                default_index = 0
            else:
                st.caption("No match found - will be treated as a new person")
                default_index = 2

            action_label = st.radio(
                "Action",
                ["Confirm match", "Choose different person", "Add as new"],
                index=default_index,
                key=f"roster_confirm_action_{c['index']}",
                horizontal=True,
            )
            chosen_name = None
            if action_label == "Choose different person":
                chosen_name = st.selectbox(
                    "Which existing person?", roster_names, key=f"roster_confirm_other_{c['index']}"
                )
            rows.append({"index": c["index"], "action_label": action_label, "chosen_name": chosen_name})
            st.divider()

        submitted = st.form_submit_button("Confirm all")

    if submitted:
        decisions = []
        for row in rows:
            if row["action_label"] == "Confirm match":
                decisions.append({"index": row["index"], "action": "confirm"})
            elif row["action_label"] == "Choose different person":
                decisions.append(
                    {
                        "index": row["index"],
                        "action": "choose_other",
                        "person_id": roster_id_by_name[row["chosen_name"]],
                    }
                )
            else:
                decisions.append({"index": row["index"], "action": "new"})
        _resume_graph({"decisions": decisions})


def _render_completeness_gate(payload: dict):
    """Blocking screen for roster_completeness_check_node's interrupt: lists
    exactly who is missing what, and lets the boss re-check once fixed on
    the Roster tab."""
    st.error(payload["message"])
    for row in payload["incomplete_rows"]:
        st.write(f"- **{row['name']}**: missing {', '.join(row['missing_fields'])}")
    st.info("Fix these on the Roster tab, then come back here and recheck.")
    if st.button("Recheck roster"):
        _resume_graph({})


def render_import_tab():
    st.subheader("Import Availability")
    result = st.session_state.get("last_result")
    interrupt_payload = _current_interrupt(result)

    if interrupt_payload is not None and "candidates" in interrupt_payload:
        _render_roster_confirm(interrupt_payload)
        return

    if interrupt_payload is not None and "incomplete_rows" in interrupt_payload:
        _render_completeness_gate(interrupt_payload)
        return

    if interrupt_payload is not None:
        # Reached human_review (or later) - the import/confirm/completeness
        # part of the pipeline is done, hand off to the Review & Edit tab.
        st.success("Import complete - continue in the Review & Edit tab.")
        return

    if result is not None and result.get("review_notes") and result.get("solve_result") is None:
        st.error("Pipeline ended without a feasible schedule:")
        st.code(result["review_notes"])

    uploaded_files = st.file_uploader(
        "Availability submissions (xlsx/pdf)", type=["xlsx", "pdf"], accept_multiple_files=True
    )

    if st.button("Run pipeline", disabled=not uploaded_files):
        tmpdir = tempfile.mkdtemp()
        paths = []
        for f in uploaded_files:
            path = str(Path(tmpdir) / f.name)
            with open(path, "wb") as out:
                out.write(f.getbuffer())
            paths.append(path)

        new_result = get_graph_app().invoke({"submission_file_paths": paths}, config=_config())
        st.session_state["last_result"] = new_result
        st.session_state["locks"] = []

        errors = new_result.get("ingestion_errors") or []
        for e in errors:
            st.error(e)
        st.rerun()


def render_review_tab():
    st.subheader("Review & Edit")
    result = st.session_state.get("last_result")
    if not result or not result.get("solve_result"):
        st.info("Run the pipeline first (see the Import Availability tab).")
        return

    people_lookup = {p.name: p for p in crud.list_roster()}
    grid = build_schedule_grid(result["solve_result"], people_lookup)
    st.dataframe(grid, use_container_width=True)
    st.write("Hours assigned:", result["solve_result"]["hours_assigned"])

    shortfalls = result["solve_result"].get("coverage_shortfalls") or []
    if shortfalls:
        st.warning(f"{len(shortfalls)} weekday slot(s) fell short of the 2 assistant + 2 tech target:")
        for s in shortfalls:
            st.write(f"- {s}")

    col1, col2 = st.columns(2)
    if col1.button("Approve"):
        _resume_graph({"decision": "approved"})
    if col2.button("Reject"):
        _resume_graph({"decision": "rejected"})

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
            new_result = _resume_graph({"decision": "edit", "locked_assignments": locks}, rerun=False)
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


def render_output_tab():
    st.subheader("Output")
    result = st.session_state.get("last_result")
    if not result or not result.get("final_schedule"):
        st.info("No approved schedule yet - approve one in the Review & Edit tab first.")
        return

    people_lookup = {p.name: p for p in crud.list_roster()}
    grid = build_schedule_grid(result["final_schedule"], people_lookup)
    st.dataframe(grid, use_container_width=True)

    shortfalls = result["final_schedule"].get("coverage_shortfalls") or []
    if shortfalls:
        st.warning(f"{len(shortfalls)} weekday slot(s) in this approved schedule are understaffed:")
        for s in shortfalls:
            st.write(f"- {s}")

    st.download_button(
        "Download CSV", grid.to_csv().encode("utf-8"), file_name="schedule.csv", mime="text/csv"
    )


st.set_page_config(page_title="Service Desk Scheduler", layout="wide")
st.title("Service Desk Scheduler")

tab_roster, tab_import, tab_review, tab_output = st.tabs(
    ["Roster", "Import Availability", "Review & Edit", "Output"]
)
with tab_roster:
    render_roster_tab()
with tab_import:
    render_import_tab()
with tab_review:
    render_review_tab()
with tab_output:
    render_output_tab()
