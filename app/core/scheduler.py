"""Фоновий планувальник (раз на хвилину): нагадування про зустрічі, «клієнт зник», строки."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core import meetings, notify
from app.core.settings_store import get_int, template
from app.core.statuses import INTAKE_STATUSES, CaseStatus
from app.core.workflow import audit, set_status
from app.db.models import Case, Meeting, utcnow
from app.messaging import hub

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 60


async def _meeting_reminders(session: AsyncSession, now: datetime) -> None:
    rows = (
        await session.scalars(
            select(Meeting)
            .where(
                Meeting.kind == "meeting",
                Meeting.status == "scheduled",
                Meeting.starts_at > now,
                Meeting.starts_at <= now + timedelta(hours=24),
            )
            .options(selectinload(Meeting.case).selectinload(Case.client))
        )
    ).all()
    for m in rows:
        if m.case is None:
            continue
        left = m.starts_at - now
        kind = ""
        if left <= timedelta(hours=2) and not m.reminded_2:
            kind = "2"
            m.reminded_2 = m.reminded_24 = True
        elif not m.reminded_24 and m.starts_at - m.created_at > timedelta(hours=24):
            kind = "24"
            m.reminded_24 = True
        if kind:
            try:
                await hub.send_to_client(
                    session, m.case, m.case.client, await meetings.reminder_text(session, m, kind)
                )
            except Exception:
                log.exception("Не вдалося надіслати нагадування про зустріч %s", m.id)
    # Минулі зустрічі — позначаємо проведеними.
    past = (
        await session.scalars(select(Meeting).where(Meeting.status == "scheduled", Meeting.ends_at < now))
    ).all()
    for m in past:
        m.status = "done"


async def _client_gone(session: AsyncSession, now: datetime, flow) -> None:
    d1 = await get_int(session, "nudge_days_1", 2)
    d2 = await get_int(session, "nudge_days_2", 5)
    d_inactive = await get_int(session, "inactive_after_days", 7)
    cases = (
        await session.scalars(
            select(Case)
            .where(
                Case.status.in_([s.value for s in INTAKE_STATUSES]),
                Case.handoff == "assistant",
                Case.intake_step.notin_(["conflict_wait", "done", ""]),
                Case.last_client_activity < now - timedelta(days=min(d1, d2, d_inactive)),
            )
            .options(selectinload(Case.client), selectinload(Case.documents), selectinload(Case.messages))
        )
    ).all()
    for case in cases:
        idle = now - case.last_client_activity
        nudges = int((case.intake or {}).get("nudges", 0))
        try:
            if nudges >= 2 and idle >= timedelta(days=d_inactive):
                case.intake = {**case.intake, "status_before_inactive": case.status}
                await set_status(session, case, CaseStatus.INACTIVE, "bot")
                await audit(session, "bot", "client_gone", "case", case.id)
            elif nudges == 1 and idle >= timedelta(days=d2):
                await hub.send_to_client(session, case, case.client, await template(session, "nudge_2"))
                case.intake = {**case.intake, "nudges": 2}
            elif nudges == 0 and idle >= timedelta(days=d1):
                await hub.send_to_client(session, case, case.client, await template(session, "nudge_1"))
                case.intake = {**case.intake, "nudges": 1}
                if flow is not None:
                    await flow.repeat_step(session, case, case.client)
        except Exception:
            log.exception("Нагадування клієнту у справі %s не надіслано", case.id)


async def _due_warnings(session: AsyncSession, now: datetime) -> None:
    cases = (
        await session.scalars(
            select(Case).where(
                Case.status.in_([CaseStatus.IN_REVIEW, CaseStatus.PREPARING, CaseStatus.APPROVED]),
                Case.response_due.is_not(None),
                Case.response_due <= now + timedelta(hours=24),
                Case.due_warned_at.is_(None),
            )
        )
    ).all()
    for case in cases:
        case.due_warned_at = now
        await notify.notify_lawyer("due_soon", case.id)


async def tick(sm: async_sessionmaker[AsyncSession], flow=None, now: datetime | None = None) -> None:
    now = now or utcnow()
    for step in (_meeting_reminders, _due_warnings):
        async with sm() as session:
            try:
                await step(session, now)
                await session.commit()
            except Exception:
                await session.rollback()
                log.exception("Планувальник: помилка на кроці %s", step.__name__)
    async with sm() as session:
        try:
            await _client_gone(session, now, flow)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("Планувальник: помилка на кроці _client_gone")


async def run_forever(sm: async_sessionmaker[AsyncSession], flow=None) -> None:
    while True:
        await tick(sm, flow)
        await asyncio.sleep(INTERVAL_SECONDS)
