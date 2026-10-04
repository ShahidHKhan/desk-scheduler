"""
Runs the scheduling pipeline on behalf of a UI.

Owns the compiled graph, its checkpointer and the one in-flight run, so a
front end only calls start_run(), current_state() and resume(). Free of
any UI framework, like the rest of scheduler/: the web app (web/) calls
these and nothing else of the pipeline.

Single-user tool: one in-flight run at a time is an accepted
simplification, not an oversight. Every call talks to the same thread,
and start_run() wipes that thread's state before a new run begins.
"""

import os
import re
import sqlite3
import threading
from functools import cache

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import create_engine

from scheduler.db.database import enable_row_level_security
from scheduler.ingest.schema import AvailabilitySubmission
from scheduler.pipeline.graph import build_graph
from scheduler.pipeline.state import PipelineState
from scheduler.solver.model_input import LockedAssignment

THREAD_ID = "main"
CONFIG = {"configurable": {"thread_id": THREAD_ID}}

# Our own classes stored in pipeline state. Listing them makes the
# checkpointer refuse to deserialize any other class. LangGraph's default
# accepts anything with a warning, and its docs say a future release will
# block unlisted classes outright - a new class added to PipelineState
# must be added here too (tests/test_pipeline_runner.py checks this).
CHECKPOINT_TYPES = [AvailabilitySubmission, LockedAssignment]

# The tables PostgresSaver.setup() creates.
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")

# Every PipelineState key set to None. A LangGraph thread keeps each state
# key from its last checkpoint unless new input overwrites it, so a run
# started without this inherits the previous run's values: its locks get
# applied to the new run's first solve, and its schedule becomes the new
# run's "last good" fallback if that solve fails.
_FRESH_STATE = dict.fromkeys(PipelineState.__annotations__)

# Held by every call that advances the run. Web requests run on
# separate threads, and two resumes of the same pause (a
# double-clicked button, a second tab) must not interleave.
_run_lock = threading.Lock()


class NoPausedRun(Exception):
    """resume() was called, but the run isn't paused waiting for input."""


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def make_checkpointer():
    """Postgres when DATABASE_URL is set (the deployed app), otherwise a
    local SQLite file - the same choice scheduler/db/database.py makes for
    the roster. Production needs Postgres: the Fly machine has no volume,
    so a local file would lose a paused review whenever the machine stops.
    """
    serde = checkpoint_serializer()
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # Imported here so local dev and tests don't need psycopg 3.
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            _libpq_url(database_url),
            min_size=1,
            max_size=4,
            # prepare_threshold=None turns off server-side prepared
            # statements, which Supabase's transaction pooler rejects.
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
            # Supabase drops idle connections; test each one before use.
            check=ConnectionPool.check_connection,
            open=True,
        )
        saver = PostgresSaver(pool, serde=serde)
        saver.setup()
        # A short-lived engine on the same database, so the same helper
        # that covers the app's own tables covers these too.
        engine = create_engine(database_url)
        try:
            enable_row_level_security(engine, CHECKPOINT_TABLES)
        finally:
            engine.dispose()
        return saver

    path = os.environ.get("CHECKPOINT_PATH", "graph_checkpoints.db")
    # One connection kept open for the life of the process, NOT
    # `with SqliteSaver.from_conn_string(...)`, which closes it as soon as
    # the block exits. check_same_thread=False because whichever thread
    # handles a request uses it; SqliteSaver serializes access itself.
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False), serde=serde)


def _libpq_url(database_url: str) -> str:
    """Drop a SQLAlchemy driver suffix (postgresql+psycopg2://...), which
    libpq doesn't understand."""
    return re.sub(r"^(postgres(?:ql)?)\+\w+://", r"\1://", database_url)


@cache
def graph_app():
    """The compiled pipeline, built once per process."""
    return build_graph(make_checkpointer())


def current_state() -> tuple[dict, dict | None]:
    """The run's latest state values, and the payload of the interrupt
    it's paused at - None when it isn't paused (finished, or never
    started). Doesn't wait on _run_lock: during a solve it returns the
    last checkpoint instead of blocking."""
    snapshot = graph_app().get_state(CONFIG)
    payload = snapshot.interrupts[0].value if snapshot.interrupts else None
    return snapshot.values, payload


def start_run(file_paths: list[str], app_submission_ids: list[int]) -> dict:
    """Start a new run from uploaded files and/or in-app submission ids,
    discarding whatever the previous run left behind. Returns the graph's
    result, with "__interrupt__" set if it paused."""
    with _run_lock:
        run_input = {
            **_FRESH_STATE,
            "submission_file_paths": file_paths,
            "app_submission_ids": app_submission_ids,
        }
        return graph_app().invoke(run_input, CONFIG)


def resume(payload: dict) -> dict:
    """Answer the interrupt the run is paused at. Raises NoPausedRun if
    it isn't paused. Returns the graph's result, as start_run() does."""
    with _run_lock:
        if current_state()[1] is None:
            raise NoPausedRun("The pipeline isn't paused, so there's nothing to continue.")
        return graph_app().invoke(Command(resume=payload), CONFIG)
