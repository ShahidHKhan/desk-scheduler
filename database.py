"""
Database engine and session setup.

Run this file directly to create roster.db with the roster table:
    python database.py
"""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from models import Base

# Reads DATABASE_PATH (see .env.example) so tests can point at an isolated
# throwaway file instead of the dev roster.db - falls back to "roster.db"
# to preserve prior behavior when unset.
DB_PATH = os.environ.get("DATABASE_PATH", "roster.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

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
