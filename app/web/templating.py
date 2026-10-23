"""Jinja2-шаблони платформи та допоміжні фільтри."""

from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.defaults import CATEGORIES
from app.core.statuses import STATUS_LABELS, CaseStatus, ConflictStatus
from app.core.workflow import is_overdue
from app.web.auth import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return ""
    # У базі час UTC; показуємо київський.
    from zoneinfo import ZoneInfo

    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Kyiv")).strftime(fmt)


templates.env.filters["dt"] = _dt
templates.env.globals.update(
    CATEGORIES=CATEGORIES,
    STATUS_LABELS={s.value: label for s, label in STATUS_LABELS.items()},
    CaseStatus=CaseStatus,
    ConflictStatus=ConflictStatus,
    is_overdue=is_overdue,
)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"text": text, "kind": kind})


def render(request: Request, name: str, **ctx) -> object:
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"csrf": csrf_token(request), "flashes": messages, **ctx}
    )
