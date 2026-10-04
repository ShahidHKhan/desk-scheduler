"""
Sign-in and role separation: worker credentials open only the availability
form, manager credentials open only the scheduler, and unset or blank
credentials never let anyone in.
"""

import pytest
from fastapi.testclient import TestClient

from web.main import app

CREDS = {
    "APP_USERNAME": "worker",
    "APP_PASSWORD": "worker-pass",
    "ADMIN_APP_USERNAME": "boss",
    "ADMIN_APP_PASSWORD": "boss-pass",
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def configured(monkeypatch):
    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)


def sign_in(client: TestClient, username: str, password: str):
    return client.post("/login", data={"username": username, "password": password})


def test_worker_sees_only_the_availability_form(client, configured):
    page = sign_in(client, "worker", "worker-pass")
    assert page.url.path == "/submit"
    assert "Submit your availability" in page.text
    assert "Import availability" not in page.text

    blocked = client.get("/manager/roster", follow_redirects=False)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/submit"


def test_manager_sees_only_the_scheduler(client, configured):
    page = sign_in(client, "boss", "boss-pass")
    assert page.url.path == "/manager/roster"
    for tab in ("Roster", "Import availability", "Review &amp; edit", "Output"):
        assert tab in page.text

    blocked = client.get("/submit", follow_redirects=False)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/manager"


def test_wrong_password_is_rejected(client, configured):
    page = sign_in(client, "boss", "worker-pass")
    assert page.status_code == 401
    assert "Invalid username or password." in page.text
    assert client.get("/manager/roster", follow_redirects=False).headers["location"] == "/login"


def test_blank_credentials_never_sign_in(client, monkeypatch):
    for name in CREDS:
        monkeypatch.setenv(name, "")
    assert sign_in(client, "", "").status_code == 401
    assert client.get("/submit", follow_redirects=False).headers["location"] == "/login"


def test_sign_out_returns_to_sign_in(client, configured):
    sign_in(client, "worker", "worker-pass")
    page = client.post("/logout")
    assert page.url.path == "/login"
    assert client.get("/submit", follow_redirects=False).headers["location"] == "/login"


def test_htmx_request_without_a_session_loads_the_sign_in_page(client):
    # A redirect would be swapped into the panel; HTMX is told to navigate instead.
    response = client.get("/manager/review", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login"
