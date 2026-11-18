"""Розділ «Зустрічі»: календар прийому, скасування, закритий для запису час."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core import meetings
from app.db.models import Case, Lawyer, Meeting, utcnow
from app.db.session import get_session
from app.integrations.google_calendar import GoogleCalendar, get_calendar
from app.web.auth import current_lawyer, verify_csrf
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(current_lawyer)])


@router.get("/meetings")
async def meetings_page(request: Request, session: AsyncSession = Depends(get_session)):
    now = utcnow()
    rows = (
        await session.scalars(
            select(Meeting)
            .where(Meeting.ends_at >= now - timedelta(days=7))
            .options(selectinload(Meeting.case).selectinload(Case.client))
            .order_by(Meeting.starts_at)
        )
    ).all()
    today = meetings.to_kyiv(now).date()
    groups: dict[str, list[Meeting]] = {
        "Сьогодні": [],
        "Найближчі 7 днів": [],
        "Пізніше": [],
        "Минулі / скасовані": [],
    }
    for m in rows:
        day = meetings.to_kyiv(m.starts_at).date()
        if m.status != "scheduled" or m.ends_at < now:
            groups["Минулі / скасовані"].append(m)
        elif day == today:
            groups["Сьогодні"].append(m)
        elif day <= today + timedelta(days=7):
            groups["Найближчі 7 днів"].append(m)
        else:
            groups["Пізніше"].append(m)
    groups["Минулі / скасовані"].sort(key=lambda m: m.starts_at, reverse=True)
    calendar = get_calendar()
    return render(
        request,
        "meetings.html",
        groups=groups,
        human_when=meetings.human_when,
        to_kyiv=meetings.to_kyiv,
        calendar_connected=calendar is not None,
        service_email=calendar.service_email if isinstance(calendar, GoogleCalendar) else "",
        calendar_configured=bool(get_settings().google_calendar_id),
        nav="meetings",
    )


@router.post("/meetings/block", dependencies=[Depends(verify_csrf)])
async def block(
    request: Request,
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    try:
        start_dt = meetings.to_utc(datetime.fromisoformat(f"{day}T{start}"))
        end_dt = meetings.to_utc(datetime.fromisoformat(f"{day}T{end}"))
        await meetings.block_time(session, start_dt, end_dt, note.strip(), f"lawyer:{lawyer.username}")
    except (ValueError, meetings.MeetingError) as exc:
        flash(request, f"Не вдалося закрити час: {exc}", "error")
        return RedirectResponse("/meetings", status_code=303)
    await session.commit()
    flash(request, "Час закрито для запису.")
    return RedirectResponse("/meetings", status_code=303)
