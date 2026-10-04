"""
Shared pytest fixtures: isolated on-disk SQLite databases for the roster
(crud.py/database.py) and the pipeline's checkpoints (pipeline/runner.py),
separate from the dev roster.db and graph_checkpoints.db.

The environment is set here, when pytest first imports this file, before
any test runs. Both the database engine and the checkpointer read it on
first use.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="scheduler_pytest_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test_roster.db")
os.environ["CHECKPOINT_PATH"] = os.path.join(_tmpdir, "test_checkpoints.db")
# Tests must never reach the real database that .env's DATABASE_URL points
# at. Set it to empty, not deleted: load_dotenv() - below, and in
# web/main.py when the web tests import it - never overrides a variable that's
# already set, but would fill in a missing one. Empty means SQLite to both
# database.py and the checkpointer.
os.environ["DATABASE_URL"] = ""

# Imports below deliberately follow the env setup above (E402).
import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import delete  # noqa: E402

# Still loads GEMINI_API_KEY etc. from .env, for tests that need a real key
# (tests/test_judge.py's live-API test).
load_dotenv()

from scheduler.db import database, models  # noqa: E402

database.init_db()


@pytest.fixture(autouse=True)
def clean_roster():
    """Every test starts with empty roster, submissions and schedules tables - tests that need rows
    add them via crud/fixtures. Runs before each test, not after, so a
    failed test's leftover rows never leak into the next one."""
    session = database.get_session()
    try:
        session.execute(delete(models.Schedule))
        session.execute(delete(models.Submission))
        session.execute(delete(models.Roster))
        session.commit()
    finally:
        session.close()
    yield
