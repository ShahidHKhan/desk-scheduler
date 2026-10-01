"""
Match parsed availability submissions against the existing roster by name.

Exact/case-insensitive matching only - deliberately not fuzzy. The human
confirm step in the pipeline catches near-misses like "J. Doe" vs "Jane
Doe" itself, so a fuzzy-matching dependency here would just add
complexity for a case a human already has to review anyway.

Ingestion proposes; it never writes to the roster.
crud.upsert_from_submission() is what actually commits, after a human
confirms each row (see graph.py's roster_confirm_node).
"""

from dataclasses import dataclass

from scheduler.db.models import Roster
from scheduler.ingest.schema import AvailabilitySubmission


@dataclass
class MatchCandidate:
    """One parsed submission plus its suggested (or absent) roster match."""

    submission: AvailabilitySubmission
    suggested_match_id: int | None
    suggested_match_name: str | None


def _normalize(name: str) -> str:
    return name.strip().lower()


def match_submissions_to_roster(
    submissions: list[AvailabilitySubmission], roster: list[Roster]
) -> list[MatchCandidate]:
    """Return one MatchCandidate per submission, in the same order."""
    by_normalized_name = {_normalize(person.name): person for person in roster}

    candidates = []
    for submission in submissions:
        existing = by_normalized_name.get(_normalize(submission.name))
        candidates.append(
            MatchCandidate(
                submission=submission,
                suggested_match_id=existing.id if existing else None,
                suggested_match_name=existing.name if existing else None,
            )
        )
    return candidates
