"""
The web app.

Run locally:
    uvicorn web.main:app --reload        # http://localhost:8000

Keep it to one process (uvicorn's default): the pipeline runner's lock,
which stops two requests advancing the same run, works within a process.
"""

import logging
import os
import secrets
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from scheduler.db import database
from web import auth
from web.routes import manager, worker
from web.views import STATIC_DIR, is_htmx

load_dotenv()

logger = logging.getLogger(__name__)

SESSION_HOURS = 12


def _session_secret() -> str:
    """SESSION_SECRET signs the session cookie. Without one, a random key
    is used, which signs everyone out whenever the process restarts."""
    secret = os.environ.get("SESSION_SECRET")
    if secret:
        return secret
    logger.warning("SESSION_SECRET is not set; sessions won't survive a restart.")
    return secrets.token_urlsafe(32)


def _not_allowed(request: Request, exc: auth.NotAllowed) -> Response:
    # An HTMX request would swap a redirect's target page into the panel,
    # so tell HTMX to load the page itself instead.
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": exc.location})
    return RedirectResponse(exc.location, status_code=303)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Creates missing tables and turns on row-level security on Postgres.
    database.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="desk_session",
        max_age=SESSION_HOURS * 3600,
        # Lax keeps the cookie off cross-site form posts, which is the
        # app's protection against cross-site request forgery.
        same_site="lax",
        # Fly sets FLY_APP_NAME; the deployed app is HTTPS-only.
        https_only=bool(os.environ.get("FLY_APP_NAME")),
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(auth.NotAllowed, _not_allowed)
    app.include_router(auth.router)
    app.include_router(worker.router)
    app.include_router(manager.router)
    return app


app = create_app()
