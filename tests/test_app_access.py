"""
Sign-in and role separation in the Streamlit app: worker credentials open
only the availability form, admin credentials open only the scheduler, and
unset or blank credentials never let anyone in.
"""

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
CREDS = {
    "APP_USERNAME": "worker",
    "APP_PASSWORD": "worker-pass",
    "ADMIN_APP_USERNAME": "boss",
    "ADMIN_APP_PASSWORD": "boss-pass",
}


@pytest.fixture(autouse=True)
def fresh_component_registration(monkeypatch):
    """The grid component registers with the Streamlit runtime when its
    module is imported. Each AppTest run gets a new runtime, so drop the
    cached module and let app.py import (and register) it again."""
    monkeypatch.delitem(sys.modules, "scheduler.ui.availability_grid", raising=False)


@pytest.fixture
def configured(monkeypatch):
    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)


def sign_in(username: str, password: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input[0].input(username)
    at.text_input[1].input(password)
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_worker_sees_only_the_availability_form(configured):
    at = sign_in("worker", "worker-pass")
    assert at.session_state["role"] == "worker"
    assert [t.value for t in at.title] == ["Submit Your Availability"]
    assert len(at.tabs) == 0
    assert any(b.label == "Submit availability" for b in at.button)


def test_admin_sees_only_the_scheduler(configured):
    at = sign_in("boss", "boss-pass")
    assert at.session_state["role"] == "admin"
    assert [t.label for t in at.tabs] == ["Roster", "Import Availability", "Review & Edit", "Output"]
    assert not any(b.label == "Submit availability" for b in at.button)


def test_wrong_password_is_rejected(configured):
    at = sign_in("boss", "worker-pass")
    assert "role" not in at.session_state
    assert [e.value for e in at.error] == ["Invalid username or password."]


def test_blank_credentials_never_sign_in(monkeypatch):
    for name in CREDS:
        monkeypatch.setenv(name, "")
    at = sign_in("", "")
    assert "role" not in at.session_state
    assert at.error


def test_sign_out_returns_to_sign_in(configured):
    at = sign_in("worker", "worker-pass")
    next(b for b in at.button if b.label == "Sign out").click().run()
    assert "role" not in at.session_state
    assert any(b.label == "Sign in" for b in at.button)
