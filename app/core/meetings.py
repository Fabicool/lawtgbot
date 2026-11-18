"""Координатор: вільні слоти, пропозиція зустрічі, запис і скасування.

Увесь час у базі — UTC; клієнту й адвокату показуємо київський.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import notify
from app.core.settings_store import get_int, get_value, template
from app.core.statuses import CaseStatus
from app.core.workflow import audit, set_status
from app.db.models import Case, Client, Meeting, utcnow
from app.integrations.google_calendar import get_calendar
from app.messaging import hub
from app.messaging.hub import Button, Keyboard

log = logging.getLogger(__name__)

KYIV = ZoneInfo("Europe/Kyiv")
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS = [
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
]


class MeetingError(RuntimeError):
    pass


def to_kyiv(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=UTC).astimezone(KYIV)


def to_utc(dt_kyiv_naive: datetime) -> datetime:
    return dt_kyiv_naive.replace(tzinfo=KYIV).astimezone(UTC).replace(tzinfo=None)


def human_when(dt_utc: datetime) -> str:
    k = to_kyiv(dt_utc)
    return f"{WEEKDAYS[k.weekday()]}, {k.day} {MONTHS[k.month - 1]} о {k:%H:%M}"


def short_when(dt_utc: datetime) -> str:
    k = to_kyiv(dt_utc)
    return f"{WEEKDAYS_SHORT[k.weekday()]} {k:%d.%m} о {k:%H:%M}"


def _hm(value: str, fallback: time) -> time:
    try:
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


async def _config(session: AsyncSession) -> dict:
    days_raw = await get_value(session, "meeting_days")
    days = {int(d) for d in days_raw.replace(" ", "").split(",") if d.isdigit() and 1 <= int(d) <= 7}
    return {
        "days": days or {1, 2, 3, 4, 5},
        "start": _hm(await get_value(session, "meeting_start"), time(10)),
        "end": _hm(await get_value(session, "meeting_end"), time(18)),
        "duration": max(15, await get_int(session, "meeting_duration", 60)),
        "notice": await get_int(session, "meeting_notice_hours", 18),
        "horizon": max(1, await get_int(session, "meeting_horizon_days", 14)),
    }


async def busy_intervals(
    session: AsyncSession, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    rows = (
        await session.scalars(
            select(Meeting).where(
                Meeting.status == "scheduled", Meeting.ends_at > start, Meeting.starts_at < end
            )
        )
    ).all()
    busy = [(m.starts_at, m.ends_at) for m in rows]
    calendar = get_calendar()
    if calendar is not None:
        try:
            busy += await calendar.busy(start, end)
        except Exception:
            log.exception("Google Calendar недоступний — враховую лише зустрічі з платформи")
    return busy


def _overlaps(a_start, a_end, intervals) -> bool:
    return any(a_start < b_end and b_start < a_end for b_start, b_end in intervals)


async def free_slots(session: AsyncSession, count: int = 3, now: datetime | None = None) -> list[datetime]:
    """До `count` вільних слотів, по можливості в різні дні. Повертає UTC."""
    cfg = await _config(session)
    now = now or utcnow()
    earliest = now + timedelta(hours=cfg["notice"])
    horizon_end = now + timedelta(days=cfg["horizon"] + 1)
    busy = await busy_intervals(session, now, horizon_end)
    duration = timedelta(minutes=cfg["duration"])

    by_day: list[list[datetime]] = []
    day: date = to_kyiv(now).date()
    for _ in range(cfg["horizon"] + 1):
        if day.isoweekday() in cfg["days"]:
            slots = []
            cursor = datetime.combine(day, cfg["start"])
            last = datetime.combine(day, cfg["end"]) - duration
            while cursor <= last:
                start_utc = to_utc(cursor)
                if start_utc >= earliest and not _overlaps(start_utc, start_utc + duration, busy):
                    slots.append(start_utc)
                cursor += duration
            if slots:
                by_day.append(slots)
        day += timedelta(days=1)

    result: list[datetime] = []
    depth = 0
    while len(result) < count and any(len(d) > depth for d in by_day):
        for slots in by_day:
            if len(slots) > depth and len(result) < count:
                result.append(slots[depth])
        depth += 1
    return sorted(result)


async def offer(
    session: AsyncSession, case: Case, client: Client, fmt: str = "office", note: str = ""
) -> list[datetime]:
    if fmt == "online" and not (await get_value(session, "online_meeting_url")).strip():
        raise MeetingError("Для онлайн-зустрічей вкажіть посилання в «Налаштуваннях».")
    if fmt == "office" and not (await get_value(session, "office_address")).strip():
        raise MeetingError("Вкажіть адресу офісу в «Налаштуваннях».")
    slots = await free_slots(session)
    if not slots:
        raise MeetingError("Немає вільних слотів у найближчі дні — перевірте робочі години або календар.")
    case.meeting_offer = {"slots": [s.isoformat() for s in slots], "format": fmt, "at": utcnow().isoformat()}
    rows = [[Button(short_when(s), f"meet:{i}")] for i, s in enumerate(slots)]
    rows.append([Button("Жоден не підходить", "meet:none")])
    text = await template(session, "meeting_offer", format_note=" онлайн" if fmt == "online" else "")
    if note:
        text = f"{note}\n\n{text}"
    await hub.send_to_client(session, case, client, text, author="assistant", keyboard=Keyboard(rows=rows))
    await audit(session, "system", "meeting_offer", "case", case.id, fmt)
    return slots


async def _where(session: AsyncSession, fmt: str) -> str:
    if fmt == "online":
        return f"Посилання: {await get_value(session, 'online_meeting_url')}"
    return f"Адреса: {await get_value(session, 'office_address')}"


async def map_url(session: AsyncSession) -> str:
    custom = (await get_value(session, "office_map_url")).strip()
    if custom:
        return custom
    address = (await get_value(session, "office_address")).strip()
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}" if address else ""


async def book(session: AsyncSession, case: Case, client: Client, index: int) -> Meeting | None:
    """Клієнт обрав слот. None — якщо слот уже зайнятий (клієнту надіслано нові варіанти)."""
    offer_data = case.meeting_offer or {}
    slots = offer_data.get("slots") or []
    if not (0 <= index < len(slots)):
        await hub.send_to_client(
            session, case, client, "Ця пропозиція вже неактуальна. Адвокат надішле нову."
        )
        return None
    fmt = offer_data.get("format", "office")
    start = datetime.fromisoformat(slots[index])
    cfg = await _config(session)
    end = start + timedelta(minutes=cfg["duration"])
    if start < utcnow() or _overlaps(start, end, await busy_intervals(session, start, end)):
        note = await template(session, "meeting_slot_taken")
        try:
            await offer(session, case, client, fmt, note=note)
        except MeetingError:
            await hub.send_to_client(session, case, client, await template(session, "meeting_none_fit"))
            await notify.notify_lawyer("meeting_none_fit", case.id)
        return None

    meeting = Meeting(case_id=case.id, kind="meeting", format=fmt, starts_at=start, ends_at=end)
    session.add(meeting)
    calendar = get_calendar()
    if calendar is not None:
        address = await get_value(session, "office_address")
        url = f"{get_settings().base_url.rstrip('/')}/cases/{case.id}"
        try:
            meeting.google_event_id = await calendar.create_event(
                start,
                end,
                summary=f"Консультація: {client.full_name or 'клієнт'}"
                + (" (онлайн)" if fmt == "online" else ""),
                description=f"Звернення #{case.id}: {url}",
                location=await get_value(session, "online_meeting_url") if fmt == "online" else address,
            )
        except Exception:
            log.exception("Не вдалося створити подію в Google Calendar")
            meeting.note = "Подію в Google Calendar не створено — додайте вручну."
    case.meeting_offer = {}
    await set_status(session, case, CaseStatus.MEETING, "bot", force=True)
    bring = await get_value(session, "meeting_bring")
    if fmt == "online":
        text = await template(
            session,
            "meeting_confirmed_online",
            when=human_when(start),
            bring=bring,
            online_url=await get_value(session, "online_meeting_url"),
        )
    else:
        text = await template(
            session,
            "meeting_confirmed",
            when=human_when(start),
            bring=bring,
            address=await get_value(session, "office_address"),
            map_url=await map_url(session),
        )
    await hub.send_to_client(session, case, client, text)
    case.unread = True
    await audit(session, "bot", "meeting_booked", "case", case.id, start.isoformat())
    await notify.notify_lawyer("meeting_booked", case.id, f"Час: {human_when(start)}")
    return meeting


async def decline_all(session: AsyncSession, case: Case, client: Client) -> None:
    case.meeting_offer = {}
    case.unread = True
    hub.record_note(
        session, case, "Клієнту не підійшов жоден із запропонованих часів зустрічі.", author="system"
    )
    await hub.send_to_client(session, case, client, await template(session, "meeting_none_fit"))
    await notify.notify_lawyer("meeting_none_fit", case.id)


async def cancel(session: AsyncSession, meeting: Meeting, actor: str, *, notify_client: bool = True) -> None:
    meeting.status = "cancelled"
    calendar = get_calendar()
    if calendar is not None and meeting.google_event_id:
        try:
            await calendar.delete_event(meeting.google_event_id)
        except Exception:
            log.exception("Не вдалося видалити подію з Google Calendar")
    case = meeting.case
    if case is not None and meeting.kind == "meeting":
        if notify_client and meeting.starts_at > utcnow():
            text = await template(session, "meeting_cancelled", when=human_when(meeting.starts_at))
            await hub.send_to_client(session, case, case.client, text, author="lawyer")
        if case.status == CaseStatus.MEETING:
            await set_status(session, case, CaseStatus.SENT, actor, force=True)
    await audit(session, actor, "meeting_cancel", "meeting", meeting.id)


async def block_time(session: AsyncSession, start: datetime, end: datetime, note: str, actor: str) -> Meeting:
    if end <= start:
        raise MeetingError("Кінець має бути пізніше за початок.")
    meeting = Meeting(kind="block", starts_at=start, ends_at=end, note=note)
    session.add(meeting)
    calendar = get_calendar()
    if calendar is not None:
        try:
            meeting.google_event_id = await calendar.create_event(
                start, end, summary="Закрито для запису", description=note, location=""
            )
        except Exception:
            log.exception("Не вдалося створити подію в Google Calendar")
    await audit(session, actor, "block_time", "meeting", None, f"{start.isoformat()} – {end.isoformat()}")
    return meeting


async def reminder_text(session: AsyncSession, meeting: Meeting, kind: str) -> str:
    return await template(
        session,
        f"meeting_reminder_{kind}",
        when=human_when(meeting.starts_at),
        where=await _where(session, meeting.format),
    )
