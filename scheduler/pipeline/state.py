"""
Shared state for the orchestration graph.
"""

from typing import TypedDict

from scheduler.ingest.schema import AvailabilitySubmission
from scheduler.solver.model_input import LockedAssignment


class PipelineState(TypedDict, total=False):
    # --- inputs ---
    submission_file_paths: list[str]
    # Ids of in-app submissions (submissions table) the boss chose to include.
    app_submission_ids: list[int]

    # --- ingestion stage ---
    availability_submissions: list[AvailabilitySubmission]
    ingestion_errors: list[str]
    ingestion_warnings: list[str]

    # --- validation stage ---
    validation_errors: list[str]

    # --- roster-confirm stage ---
    # availability_submissions index -> committed roster person id, set by
    # roster_confirm_node once the boss has confirmed/created each row.
    submission_roster_ids: dict[int, int]

    # --- roster-completeness gate ---
    # Empty once the gate has passed; only meaningful mid-interrupt.
    roster_incomplete_rows: list[dict]

    # --- solve stage ---
    solve_status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN" | "LOCK_CONFLICT" | "SKIPPED"
    solve_result: dict | None
    infeasibility_gaps: list[str]
    # The manual edits solve_result was solved with. Only a successful
    # solve changes this, so it always matches the schedule on screen.
    locked_assignments: list[LockedAssignment]
    # Set by human_review_node on an "edit": the lock list to try next.
    # solve_node consumes it - it becomes locked_assignments if the solve
    # succeeds, and is discarded if it fails.
    proposed_locks: list[LockedAssignment] | None
    lock_conflicts: list[str]

    # --- human review stage ---
    review_decision: str  # "approved" | "rejected" | "edit" | "" (pending)
    review_notes: str

    # --- output ---
    final_schedule: dict | None
    # Id of the row output_node saved to the schedules table.
    schedule_id: int | None
