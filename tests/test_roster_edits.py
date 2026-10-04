"""
crud.update_person() changes only the fields the manager edits on the
roster - never the id, and never the availability a confirmed submission
wrote.
"""

import pytest

from scheduler.db import crud
from scheduler.db.crud import RosterValidationError


def test_editable_fields_update():
    person = crud.add_person("Jane Doe", "JD", "tech_only", 2, 2, 10)
    updated = crud.update_person(person.id, name="Jane Smith", hours_requested=12, role_weighting="hybrid_new")
    assert (updated.name, updated.hours_requested, updated.role_weighting) == ("Jane Smith", 12, "hybrid_new")


@pytest.mark.parametrize("field, value", [("id", 999), ("availability_json", "{}"), ("is_complete", True)])
def test_other_fields_are_rejected(field, value):
    person = crud.add_person("Jane Doe", "JD", "tech_only", 2, 2, 10)
    with pytest.raises(RosterValidationError, match=field):
        crud.update_person(person.id, **{field: value})
    unchanged = crud.get_person(person.id)
    assert unchanged is not None and unchanged.availability_json is None


def test_unknown_role_weighting_is_rejected_with_a_clear_message():
    person = crud.add_person("Jane Doe", "JD", "tech_only", 2, 2, 10)
    with pytest.raises(RosterValidationError, match="role_weighting must be one of"):
        crud.update_person(person.id, role_weighting="manager")
