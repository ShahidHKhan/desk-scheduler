"""
Template setup and the small helpers every route shares.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from scheduler.solver.model_input import slot_label, time_label

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=WEB_DIR / "templates")

ROLE_WEIGHTING_LABELS = {
    "assistant_only": "Assistant only",
    "hybrid_new": "Hybrid, new",
    "hybrid_2nd": "Hybrid, 2nd year",
    "tech_only": "Tech only",
}

# How the roster's manual fields read on screen.
FIELD_LABELS = {
    "role_weighting": "role",
    "experience_rating": "experience rating",
    "proximity": "proximity",
    "initials": "initials",
}

templates.env.globals.update(
    time_label=time_label,
    slot_label=slot_label,
    role_weighting_label=lambda value: ROLE_WEIGHTING_LABELS.get(value, "Role not set"),
)
templates.env.filters["field_label"] = lambda field: FIELD_LABELS.get(field, field)


def is_htmx(request: Request) -> bool:
    """True for a request HTMX made to swap part of a page, rather than a
    full page load."""
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template: str, /, *, status_code: int = 200, headers: dict | None = None, **context):
    """Render a template; everything else passed by keyword is its context
    (positional-only above, so a form field called "name" can't clash)."""
    context.setdefault("role", request.session.get("role"))
    context.setdefault("messages", [])
    return templates.TemplateResponse(request, template, context, status_code=status_code, headers=headers)


def local_time(moment: datetime) -> str:
    """A stored UTC timestamp in the desk's timezone (APP_TIMEZONE). SQLite
    hands timestamps back without tzinfo, so a naive value is treated as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    zone = ZoneInfo(os.getenv("APP_TIMEZONE", "America/New_York"))
    return moment.astimezone(zone).strftime("%b %d, %I:%M %p")
