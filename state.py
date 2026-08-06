"""
Shared state for the orchestration graph.
"""

from typing import TypedDict

from model_input import LockedAssignment
from schema import AvailabilitySubmission


class PipelineState(TypedDict, total=False):
    # --- inputs ---
    submission_file_paths: list[str]

    # --- ingestion stage ---
    availability_submissions: list[AvailabilitySubmission]
    ingestion_errors: list[str]
    ingestion_warnings: list[str]

    # --- validation stage ---
    validation_errors: list[str]

    # --- roster-confirm stage (Section 4b, step 3) ---
    # availability_submissions index -> committed roster person id, set by
    # roster_confirm_node once the boss has confirmed/created each row.
    submission_roster_ids: dict[int, int]

    # --- roster-completeness gate (Section 4b, step 5) ---
    # Empty once the gate has passed; only meaningful mid-interrupt.
    roster_incomplete_rows: list[dict]

    # --- solve stage ---
    solve_status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN" | "LOCK_CONFLICT" | "SKIPPED"
    solve_result: dict | None
    infeasibility_gaps: list[str]
    locked_assignments: list[LockedAssignment]
    lock_conflicts: list[str]

    # --- human review stage ---
    review_decision: str  # "approved" | "rejected" | "edit" | "" (pending)
    review_notes: str

    # --- output ---
    final_schedule: dict | None
