"""
Database engine and session setup.

Run this module directly to create roster.db with its tables:
    python -m scheduler.db.database
"""

import logging
import os
from collections.abc import Iterable
from functools import cache

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from scheduler.db.models import Base

logger = logging.getLogger(__name__)


def database_url() -> str:
    """DATABASE_URL (see .env.example) when set: the deployed Postgres
    (Supabase) instance. Otherwise local SQLite at DATABASE_PATH, default
    roster.db. Tests blank DATABASE_URL and point DATABASE_PATH at a temp
    file (see tests/conftest.py)."""
    return os.environ.get("DATABASE_URL") or f"sqlite:///{os.environ.get('DATABASE_PATH', 'roster.db')}"


@cache
def get_engine() -> Engine:
    """The engine, created on first use rather than at import time, so it
    reflects the environment once the entry point has loaded .env -
    whatever order modules happen to be imported in."""
    return create_engine(database_url(), echo=False)


@cache
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine())


def get_session() -> Session:
    """Return a new session for talking to the database."""
    return _session_factory()()


def init_db() -> None:
    """Create all tables that don't already exist, and add any columns that
    were added to the model after a database file was first created (SQLite
    has no built-in migration tool, and Base.metadata.create_all() only
    creates missing tables, not missing columns on existing ones). Safe to
    call repeatedly.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    enable_row_level_security(engine, Base.metadata.tables)

    inspector = inspect(engine)
    if "roster" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("roster")}
    if "availability_json" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE roster ADD COLUMN availability_json VARCHAR"))



def enable_row_level_security(engine: Engine, table_names: Iterable[str]) -> None:
    """Turn on row-level security for these tables, where it's off. Postgres
    only; a no-op on SQLite, and for any table that doesn't exist yet.

    Supabase serves every table in the public schema through its built-in
    REST API. Row-level security with no policies closes that off. The app
    is unaffected: it connects as the role that created these tables, and
    row-level security doesn't apply to a table's owner.

    A failure (say, a table owned by another role) is logged, not raised -
    it shouldn't stop the app from starting.
    """
    if engine.dialect.name != "postgresql":
        return
    for table in table_names:
        try:
            with engine.begin() as conn:
                enabled = conn.execute(
                    text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:table)"),
                    {"table": table},
                ).scalar()
                # None: the table doesn't exist.
                if enabled is False:
                    conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        except SQLAlchemyError as e:
            logger.warning("Could not enable row-level security on %s: %s", table, e)


if __name__ == "__main__":
    init_db()
    url = database_url()
    # Never print a Postgres URL: it carries the password.
    print(f"Initialized {url if url.startswith('sqlite') else 'the DATABASE_URL database'}.")
