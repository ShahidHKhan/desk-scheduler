"""
CRUD functions for the roster, in-app submissions, approved schedules and
settings.

Plain functions, not tied to any particular UI framework, so the
web app, the pipeline graph, and scripts can all call them
directly. Each function opens and closes its own session.
"""

import json

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from scheduler.db.database import get_session
from scheduler.db.models import (
    EDITABLE_FIELDS,
    REQUIRED_FIELDS,
    ROLE_WEIGHTINGS,
    SETTING_KEYS,
    SUBMISSION_STATUSES,
    Roster,
    Schedule,
    Setting,
    Submission,
)


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
    """Return roster rows missing any REQUIRED_FIELDS field.

    Backs the pipeline's pre-solve completeness gate and the Roster tab's
    "Show incomplete only" filter.
    """
    session = get_session()
    try:
        conditions = [getattr(Roster, field).is_(None) for field in REQUIRED_FIELDS]
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
    initials: str | None = None,
) -> Roster:
    """Commit one confirmed roster-confirm decision (see graph.roster_confirm_node).

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
    availability at solve time; see
    tests/test_roster_availability_persistence.py for the regression test.

    person_id None ("no match, treat as new"): create a fresh row with
    name + hours_requested (+ availability) set and the four manual fields
    left NULL - incomplete until the boss fills them in on the roster.

    initials comes only from in-app submissions, where the worker types
    them in. It fills a new row, or an existing row whose initials are
    still unset - it never replaces initials the boss already chose.
    """
    availability_json = json.dumps(availability) if availability is not None else None

    session = get_session()
    try:
        if person_id is not None:
            person = session.get(Roster, person_id)
            if person is None:
                raise RosterValidationError(f"No person with id={person_id}")
            # A form whose hours couldn't be read mustn't wipe hours the
            # boss already set.
            if hours_requested is not None:
                person.hours_requested = hours_requested
            if availability_json is not None:
                person.availability_json = availability_json
            if initials and not person.initials:
                person.initials = initials
        else:
            person = Roster(
                name=name,
                hours_requested=hours_requested,
                availability_json=availability_json,
                initials=initials or None,
            )
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
    """Update one or more of EDITABLE_FIELDS for an existing person.

    Example: update_person(3, hours_requested=12, role_weighting="hybrid_2nd")
    Raises RosterValidationError if the person doesn't exist, a field isn't
    editable, or the update violates a constraint.
    """
    not_editable = sorted(set(fields) - set(EDITABLE_FIELDS))
    if not_editable:
        raise RosterValidationError(f"These fields can't be edited: {', '.join(not_editable)}")
    role_weighting = fields.get("role_weighting")
    if role_weighting is not None and role_weighting not in ROLE_WEIGHTINGS:
        raise RosterValidationError(
            f"role_weighting must be one of {ROLE_WEIGHTINGS}, got {role_weighting!r}"
        )

    session = get_session()
    try:
        person = session.get(Roster, person_id)
        if person is None:
            raise RosterValidationError(f"No person with id={person_id}")

        for key, value in fields.items():
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


# --- In-app availability submissions --------------------------------------


def create_submission(
    name: str, initials: str, hours_requested: int, availability: dict[str, list[bool]]
) -> Submission:
    """Store one in-app availability form as a pending submission.
    Raises RosterValidationError on bad input."""
    name, initials = name.strip(), initials.strip().upper()
    if not name:
        raise RosterValidationError("Name is required.")
    if not initials:
        raise RosterValidationError("Initials are required.")

    session = get_session()
    try:
        submission = Submission(
            name=name,
            initials=initials,
            hours_requested=hours_requested,
            availability_json=json.dumps(availability),
        )
        session.add(submission)
        session.commit()
        session.refresh(submission)
        return submission
    except IntegrityError as e:
        session.rollback()
        raise RosterValidationError(f"Could not save submission for {name!r}: {e.orig}") from e
    finally:
        session.close()


def get_submission(submission_id: int) -> Submission | None:
    session = get_session()
    try:
        return session.get(Submission, submission_id)
    finally:
        session.close()


def list_submissions(status: str | None = "pending") -> list[Submission]:
    """Submissions with the given status (all of them if None), newest first."""
    session = get_session()
    try:
        query = select(Submission).order_by(Submission.submitted_at.desc(), Submission.id.desc())
        if status is not None:
            query = query.where(Submission.status == status)
        return list(session.execute(query).scalars())
    finally:
        session.close()


def set_submission_status(submission_id: int, status: str, roster_id: int | None = None) -> Submission:
    """Move a submission to "imported" (with the roster row it became) or
    "dismissed"."""
    if status not in SUBMISSION_STATUSES:
        raise RosterValidationError(f"status must be one of {SUBMISSION_STATUSES}, got {status!r}")

    session = get_session()
    try:
        submission = session.get(Submission, submission_id)
        if submission is None:
            raise RosterValidationError(f"No submission with id={submission_id}")
        submission.status = status
        if roster_id is not None:
            submission.roster_id = roster_id
        session.commit()
        session.refresh(submission)
        return submission
    finally:
        session.close()


# --- Approved schedules ---------------------------------------------------


def save_schedule(result: dict, locks: list[dict]) -> Schedule:
    """Save an approved solver result and the locks it was solved with."""
    session = get_session()
    try:
        schedule = Schedule(result_json=json.dumps(result), locks_json=json.dumps(locks))
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule
    finally:
        session.close()


def latest_schedule() -> Schedule | None:
    """The most recently approved schedule, or None if nothing's been approved yet."""
    session = get_session()
    try:
        query = select(Schedule).order_by(Schedule.approved_at.desc(), Schedule.id.desc()).limit(1)
        return session.execute(query).scalars().first()
    finally:
        session.close()


# --- Settings -----------------------------------------------------------------


def get_settings() -> dict[str, str]:
    """Every setting in SETTING_KEYS; one never saved reads as ""."""
    session = get_session()
    try:
        saved = {row.key: row.value for row in session.execute(select(Setting)).scalars()}
        return {key: saved.get(key, "") for key in SETTING_KEYS}
    finally:
        session.close()


def set_setting(key: str, value: str) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"Unknown setting {key!r}")
    session = get_session()
    try:
        session.merge(Setting(key=key, value=value))
        session.commit()
    finally:
        session.close()


# --- Starting over --------------------------------------------------------------


def delete_everything() -> None:
    """Empty every table: the roster, submissions, approved schedules and
    settings. For testing - see the manager's reset route."""
    session = get_session()
    try:
        for model in (Schedule, Setting, Submission, Roster):
            session.execute(delete(model))
        session.commit()
    finally:
        session.close()
