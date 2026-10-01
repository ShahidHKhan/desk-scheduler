"""
Database engine and session setup.

Run this module directly to create roster.db with the roster table:
    python -m scheduler.db.database
"""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from scheduler.db.models import Base

# DATABASE_URL (see .env.example), when set, points at the deployed Postgres
# (Supabase) instance used in production. When unset, falls back to local
# SQLite via DATABASE_PATH: tests set
# DATABASE_PATH (see tests/conftest.py) and never DATABASE_URL, so the
# pytest suite is unaffected by this fallback and keeps running against
# local SQLite regardless of this branch.
DB_PATH = os.environ.get("DATABASE_PATH", "roster.db")
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, echo=False)

SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    """Return a new session for talking to the database."""
    return SessionLocal()


def init_db() -> None:
    """Create all tables that don't already exist, and add any columns that
    were added to the model after a database file was first created (SQLite
    has no built-in migration tool, and Base.metadata.create_all() only
    creates missing tables, not missing columns on existing ones). Safe to
    call repeatedly.
    """
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    if "roster" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("roster")}
    if "availability_json" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE roster ADD COLUMN availability_json VARCHAR"))


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH} with the roster table.")
