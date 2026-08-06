"""
CRUD functions for the roster table.

Plain functions, not tied to any particular UI framework, so the
Phase 5 review UI (or a quick CLI/script in the meantime) can call
these directly. Each function opens and closes its own session.
"""

import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, select

from database import get_session
from models import MANUAL_FIELDS, Roster, ROLE_WEIGHTINGS


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


def list_incomplete_roster() -> list[Roster]:
    """Return roster rows missing any of the four boss-set-manually fields.

    Backs the Phase 4 solve-time hard gate and the Phase 5 roster panel's
    "Incomplete" filter.
    """
    session = get_session()
    try:
        conditions = [getattr(Roster, field).is_(None) for field in MANUAL_FIELDS]
        rows = session.execute(
            select(Roster).where(or_(*conditions)).order_by(Roster.name)
        ).scalars()
        return list(rows)
    finally:
        session.close()


def upsert_from_submission(
    person_id: int | None,
    name: str,
    hours_requested: int | None,
    availability: dict[str, list[bool]] | None = None,
) -> Roster:
    """Commit one confirmed roster-confirm decision (see Section 4b, step 3).

    person_id given (a confirmed or manually-chosen match): update ONLY
    hours_requested and availability on that existing row. Never touches
    role_weighting, experience_rating, proximity, or initials - those are
    set by the boss directly and the submitted forms never carry them, so a
    wrong match must not be able to clobber a returning person's
    already-set attributes.

    availability is persisted (not just held in this run's pipeline state)
    so that a solve triggered by a LATER run - one that doesn't re-upload
    this person's file - still has their availability to work with. Without
    this, only whoever's file was part of the current run would have any
    availability at solve time; see Schedule_Optimizer_Project_Notes.md /
    the Phase 6 regression test for the bug this caused.

    person_id None ("no match, treat as new"): create a fresh row with
    name + hours_requested (+ availability) set and the four manual fields
    left NULL - incomplete until the boss fills them in on the roster.
    """
    availability_json = json.dumps(availability) if availability is not None else None

    session = get_session()
    try:
        if person_id is not None:
            person = session.get(Roster, person_id)
            if person is None:
                raise RosterValidationError(f"No person with id={person_id}")
            person.hours_requested = hours_requested
            if availability_json is not None:
                person.availability_json = availability_json
        else:
            person = Roster(name=name, hours_requested=hours_requested, availability_json=availability_json)
            session.add(person)

        session.commit()
        session.refresh(person)
        return person
    except IntegrityError as e:
        session.rollback()
        raise RosterValidationError(f"Could not upsert {name!r}: {e.orig}") from e
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
