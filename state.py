"""
Shared state for the orchestration graph.
"""

from typing import TypedDict

from model_input import Person
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
    solve_status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN"
    solve_result: dict | None
    infeasibility_gaps: list[str]

    # --- human review stage ---
    review_decision: str  # "approved" | "rejected" | "" (pending)
    review_notes: str

    # --- output ---
    final_schedule: dict | None
