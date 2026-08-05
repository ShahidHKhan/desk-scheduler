"""
Shared state for the orchestration graph.
"""

from typing import TypedDict

from model_input import LockedAssignment, Person
from schema import AvailabilitySubmission


class PipelineState(TypedDict, total=False):
    # --- inputs ---
    submission_file_paths: list[str]
    roster: list[Person]

    # --- ingestion stage ---
    availability_submissions: list[AvailabilitySubmission]
    ingestion_errors: list[str]
    ingestion_warnings: list[str]

    # --- validation stage ---
    validation_errors: list[str]

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
