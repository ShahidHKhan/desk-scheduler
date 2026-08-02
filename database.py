"""
Database engine and session setup.

Run this file directly to create roster.db with the roster table:
    python database.py
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models import Base

DB_PATH = "roster.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, echo=False)

SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    """Return a new session for talking to the database."""
    return SessionLocal()


def init_db() -> None:
    """Create all tables that don't already exist. Safe to call repeatedly."""
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH} with the roster table.")
