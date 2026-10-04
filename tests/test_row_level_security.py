"""
Row-level security on Postgres (scheduler/db/database.py): the app turns it
on for every table it creates, so Supabase's built-in REST API can't read
them. The Postgres test is skipped unless TEST_POSTGRES_URL points at a
disposable database; tests/test_pipeline_runner.py covers the checkpoint
tables the same way.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from scheduler.db import database
from scheduler.db.models import Base

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def test_sqlite_is_left_alone():
    database.enable_row_level_security(database.get_engine(), Base.metadata.tables)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")
def test_app_tables_get_row_level_security():
    engine = create_engine(POSTGRES_URL)
    try:
        Base.metadata.create_all(engine)
        # Start with one table off, in case an earlier run already turned it on.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE roster DISABLE ROW LEVEL SECURITY"))

        names = list(Base.metadata.tables)
        database.enable_row_level_security(engine, [*names, "no_such_table"])
        database.enable_row_level_security(engine, names)  # already on: a no-op

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:names)"),
                {"names": names},
            ).all()
        assert dict(rows) == dict.fromkeys(names, True)
    finally:
        engine.dispose()
