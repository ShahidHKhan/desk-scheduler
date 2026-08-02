"""
CRUD functions for the roster table.

Plain functions, not tied to any particular UI framework, so the
Phase 5 review UI (or a quick CLI/script in the meantime) can call
these directly. Each function opens and closes its own session.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from database import get_session
from models import Roster, ROLE_WEIGHTINGS


class RosterValidationError(Exception):
    """Raised when a roster entry violates a CHECK constraint or is otherwise invalid."""


def add_person(
    name: str,
    initials: str,
    role_weighting: str,
    experience_rating: int,
    proximity: int,
    hours_requested: int,
) -> Roster:
    """Add a new person to the roster. Raises RosterValidationError on bad input."""
    if role_weighting not in ROLE_WEIGHTINGS:
        raise RosterValidationError(
            f"role_weighting must be one of {ROLE_WEIGHTINGS}, got {role_weighting!r}"
        )

    session = get_session()
    try:
        person = Roster(
            name=name,
            initials=initials,
            role_weighting=role_weighting,
            experience_rating=experience_rating,
            proximity=proximity,
            hours_requested=hours_requested,
        )
        session.add(person)
        session.commit()
        session.refresh(person)
        return person
    except IntegrityError as e:
        session.rollback()
        raise RosterValidationError(f"Could not add {name!r}: {e.orig}") from e
    finally:
        session.close()


def get_person(person_id: int) -> Roster | None:
    """Fetch a single person by id, or None if not found."""
    session = get_session()
    try:
        return session.get(Roster, person_id)
    finally:
        session.close()


def list_roster() -> list[Roster]:
    """Return every person on the roster, ordered by name."""
    session = get_session()
    try:
        return list(session.execute(select(Roster).order_by(Roster.name)).scalars())
    finally:
        session.close()


def update_person(person_id: int, **fields) -> Roster:
    """Update one or more fields for an existing person.

    Example: update_person(3, hours_requested=12, role_weighting="hybrid_2nd")
    Raises RosterValidationError if the person doesn't exist or the update
    violates a constraint.
    """
    session = get_session()
    try:
        person = session.get(Roster, person_id)
        if person is None:
            raise RosterValidationError(f"No person with id={person_id}")

        for key, value in fields.items():
            if not hasattr(person, key):
                raise RosterValidationError(f"Roster has no field {key!r}")
            setattr(person, key, value)

        session.commit()
        session.refresh(person)
        return person
    except IntegrityError as e:
        session.rollback()
        raise RosterValidationError(f"Could not update id={person_id}: {e.orig}") from e
    finally:
        session.close()


def delete_person(person_id: int) -> bool:
    """Remove a person from the roster. Returns True if deleted, False if not found."""
    session = get_session()
    try:
        person = session.get(Roster, person_id)
        if person is None:
            return False
        session.delete(person)
        session.commit()
        return True
    finally:
        session.close()
