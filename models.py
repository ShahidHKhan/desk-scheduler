"""
SQLAlchemy models for the Service Desk Schedule Optimizer.

Phase 1: roster / attribute data model.
See Schedule_Optimizer_Project_Notes.md, Section 7, for the decisions
this file implements (flat single table, no semester history, CHECK
constraints for fixed-category fields).
"""

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Allowed values for role_weighting — kept here as a single source of
# truth so the CHECK constraint and any app-level validation/dropdowns
# can both reference it instead of duplicating the literal strings.
ROLE_WEIGHTINGS = ("assistant_only", "hybrid_new", "hybrid_2nd", "tech_only")


class Roster(Base):
    """One row per service desk worker.

    Flat table, no semester history (confirmed decision): each new
    semester, hours_requested (and role_weighting, if someone has
    leveled up from hybrid_new to hybrid_2nd) gets overwritten
    directly on the person's existing row rather than versioned.
    """

    __tablename__ = "roster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String, nullable=False)

    # Short initials used on the master schedule grid (e.g. "SK" for
    # Shahid Khan) — needed to match roster entries against the
    # existing master schedule format your boss already works with.
    initials: Mapped[str] = mapped_column(String, nullable=False)

    role_weighting: Mapped[str] = mapped_column(String, nullable=False)

    experience_rating: Mapped[int] = mapped_column(Integer, nullable=False)

    proximity: Mapped[int] = mapped_column(Integer, nullable=False)

    hours_requested: Mapped[int] = mapped_column(Integer, nullable=False)

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

    def __repr__(self) -> str:
        return (
            f"Roster(id={self.id!r}, name={self.name!r}, "
            f"initials={self.initials!r}, role_weighting={self.role_weighting!r}, "
            f"experience_rating={self.experience_rating!r}, "
            f"proximity={self.proximity!r}, hours_requested={self.hours_requested!r})"
        )
