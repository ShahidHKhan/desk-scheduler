"""
Shared pytest fixtures: an isolated on-disk SQLite DB for tests that touch
crud.py/database.py, separate from the dev roster.db.

DATABASE_PATH (read by database.py) is set here, at module import time,
before any test module gets a chance to import database/crud - crud/DB
tests need this in place before those modules bind their engine.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="scheduler_pytest_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test_roster.db")

# Imports below deliberately follow the env setup above (E402).
import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import delete  # noqa: E402

# Loaded after DATABASE_PATH above so the test DB path isn't clobbered
# (load_dotenv() never overrides an already-set env var) - but still
# populates GEMINI_API_KEY etc. from .env for tests that need a real key
# (e.g. tests/test_judge.py's live-API test), same as router.py does for
# the app itself.
load_dotenv()

# DATABASE_URL has no prior value in os.environ, so the
# load_dotenv() call above WILL pull the real Supabase URL in from .env if
# it's set there for the deployed app. Tests must never run against that -
# see database.py's DATABASE_URL-over-DATABASE_PATH fallback - so drop it
# unconditionally before database.py is imported and binds its engine.
os.environ.pop("DATABASE_URL", None)

from scheduler.db import database, models  # noqa: E402

database.init_db()


@pytest.fixture(autouse=True)
def clean_roster():
    """Every test starts with empty roster and submissions tables - tests that need rows
    add them via crud/fixtures. Runs before each test, not after, so a
    failed test's leftover rows never leak into the next one."""
    session = database.get_session()
    try:
        session.execute(delete(models.Submission))
        session.execute(delete(models.Roster))
        session.commit()
    finally:
        session.close()
    yield
