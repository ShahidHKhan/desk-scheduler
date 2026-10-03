"""
SQLAlchemy models for the Service Desk Schedule Optimizer.

A single flat roster table: no semester history, and CHECK constraints
on the fixed-category fields so bad values can't be stored.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Allowed values for role_weighting — kept here as a single source of
# truth so the CHECK constraint and any app-level validation/dropdowns
# can both reference it instead of duplicating the literal strings.
ROLE_WEIGHTINGS = ("assistant_only", "hybrid_new", "hybrid_2nd", "tech_only")

# The four fields the submitted availability forms never capture - set by
# the scheduler directly on the roster. A row missing any of these is
# "incomplete" and blocks solve.
MANUAL_FIELDS = ("role_weighting", "experience_rating", "proximity", "initials")

# Lifecycle of an in-app availability submission (see Submission below).
SUBMISSION_STATUSES = ("pending", "imported", "dismissed")


class Roster(Base):
    """One row per service desk worker.

    Flat table, no semester history: each new
    semester, hours_requested (and role_weighting, if someone has
    leveled up from hybrid_new to hybrid_2nd) gets overwritten
    directly on the person's existing row rather than versioned.

    The roster is now generated FROM bulk-uploaded availability
    submissions rather than typed in by hand first. A
    submission only ever populates name + hours_requested; the four
    MANUAL_FIELDS are nullable here specifically so a freshly-created
    row can sit "incomplete" until they're filled in by hand -
    see is_complete below and crud.upsert_from_submission().
    """

    __tablename__ = "roster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String, nullable=False)

    # Short initials used on the master schedule grid (e.g. "JD" for
    # Jane Doe), matching the existing hand-built master schedule format.
    initials: Mapped[str | None] = mapped_column(String, nullable=True)

    role_weighting: Mapped[str | None] = mapped_column(String, nullable=True)

    experience_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)

    proximity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Nullable because a parsed submission can itself have an unfilled
    # hours field (schema.AvailabilitySubmission.hours_requested is
    # int | None) - the roster row shouldn't fail to be created just
    # because that one value is missing.
    hours_requested: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Persisted availability, JSON-serialized dict[str, list[bool]] (day ->
    # per-slot booleans, same shape as schema.AvailabilitySubmission.availability
    # and model_input.Person.availability). Without this column, availability
    # only ever lived in a single pipeline run's ephemeral state - a solve
    # triggered by a run that only re-uploaded one person's corrected file
    # would build every OTHER roster member's Person with availability={}
    # (nothing marked available anywhere), silently dropping them from the
    # solve. Persisting it here means a person's availability survives across
    # runs and is only overwritten when *their* submission is re-parsed and
    # re-confirmed (see crud.upsert_from_submission()).
    availability_json: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"role_weighting IN {ROLE_WEIGHTINGS}",
            name="ck_roster_role_weighting",
        ),
        CheckConstraint(
            "experience_rating BETWEEN 1 AND 4",
            name="ck_roster_experience_rating",
        ),
        CheckConstraint(
            "proximity BETWEEN 1 AND 3",
            name="ck_roster_proximity",
        ),
        CheckConstraint(
            "hours_requested BETWEEN 3 AND 20",
            name="ck_roster_hours_requested",
        ),
    )

    @property
    def is_complete(self) -> bool:
        """True only when all four boss-set-manually fields are filled in."""
        return all(getattr(self, field) is not None for field in MANUAL_FIELDS)

    @property
    def missing_fields(self) -> list[str]:
        """Which of the four manual fields are still unset, if any."""
        return [field for field in MANUAL_FIELDS if getattr(self, field) is None]

    @property
    def availability(self) -> dict[str, list[bool]]:
        """Deserialized availability_json, or {} if never set."""
        if not self.availability_json:
            return {}
        return json.loads(self.availability_json)

    def __repr__(self) -> str:
        return (
            f"Roster(id={self.id!r}, name={self.name!r}, "
            f"initials={self.initials!r}, role_weighting={self.role_weighting!r}, "
            f"experience_rating={self.experience_rating!r}, "
            f"proximity={self.proximity!r}, hours_requested={self.hours_requested!r})"
        )


class Submission(Base):
    """An availability form filled in through the app, instead of an
    uploaded xlsx/pdf.

    Kept separate from Roster on purpose: a submission is the worker's
    request, and nothing reaches the roster until the manager includes it
    in a pipeline run and confirms the name match there - the same path an
    uploaded file takes. `status` tracks that: "pending" until a run
    commits it (then "imported", with roster_id set), or "dismissed" if
    the manager discards it. Workers can submit again; each submission is
    a new row, so the history of what they asked for is kept.
    """

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    initials: Mapped[str] = mapped_column(String, nullable=False)
    hours_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    # Same JSON shape as Roster.availability_json: day -> per-slot booleans.
    availability_json: Mapped[str] = mapped_column(String, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # Set when a pipeline run commits this submission to the roster.
    roster_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("roster.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(f"status IN {SUBMISSION_STATUSES}", name="ck_submissions_status"),
        CheckConstraint("hours_requested BETWEEN 3 AND 20", name="ck_submissions_hours_requested"),
    )

    @property
    def availability(self) -> dict[str, list[bool]]:
        return json.loads(self.availability_json)

    def __repr__(self) -> str:
        return (
            f"Submission(id={self.id!r}, name={self.name!r}, initials={self.initials!r}, "
            f"hours_requested={self.hours_requested!r}, status={self.status!r})"
        )
