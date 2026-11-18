"""Google Calendar через сервісний акаунт.

Адвокат створює окремий календар «Прийом» і дає доступ сервісному акаунту («Вносити зміни
до подій»). Асистент бачить лише цей календар, особисті події — ні. Без налаштування
зустрічі ведуться лише в базі платформи.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarError(RuntimeError):
    pass


class Calendar(Protocol):
    async def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...

    async def create_event(
        self, start: datetime, end: datetime, summary: str, description: str, location: str
    ) -> str: ...

    async def delete_event(self, event_id: str) -> None: ...


def _iso(dt: datetime) -> str:
    """Час у базі — UTC без зони."""
    return dt.replace(microsecond=0).isoformat() + "Z"


def _parse(value: str) -> datetime:
    """Відповідь Google → UTC без зони."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


class GoogleCalendar:
    def __init__(self, key_file: str, calendar_id: str) -> None:
        from google.oauth2 import service_account

        self.calendar_id = calendar_id
        self._creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
        self._lock = asyncio.Lock()

    @property
    def service_email(self) -> str:
        return self._creds.service_account_email

    async def _token(self) -> str:
        async with self._lock:
            if not self._creds.valid or (
                self._creds.expiry
                and self._creds.expiry - datetime.now(UTC).replace(tzinfo=None) < timedelta(minutes=5)
            ):
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self._creds.refresh, Request())
            return self._creds.token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, f"{API}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            raise CalendarError(f"Google Calendar {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    async def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        data = await self._request(
            "POST",
            "/freeBusy",
            json={"timeMin": _iso(start), "timeMax": _iso(end), "items": [{"id": self.calendar_id}]},
        )
        cal = data.get("calendars", {}).get(self.calendar_id, {})
        if cal.get("errors"):
            raise CalendarError(f"Немає доступу до календаря: {cal['errors']}")
        return [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]

    async def create_event(
        self, start: datetime, end: datetime, summary: str, description: str, location: str
    ) -> str:
        body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": _iso(start)},
            "end": {"dateTime": _iso(end)},
        }
        data = await self._request("POST", f"/calendars/{quote(self.calendar_id)}/events", json=body)
        return data.get("id", "")

    async def delete_event(self, event_id: str) -> None:
        if event_id:
            await self._request("DELETE", f"/calendars/{quote(self.calendar_id)}/events/{quote(event_id)}")


_calendar: Calendar | None = None
_override = False


def set_calendar(calendar: Calendar | None) -> None:
    """Для тестів: підміна календаря."""
    global _calendar, _override
    _calendar, _override = calendar, True


def get_calendar() -> Calendar | None:
    global _calendar
    if _override or _calendar is not None:
        return _calendar
    s = get_settings()
    if not (s.google_service_account_file and s.google_calendar_id):
        return None
    try:
        _calendar = GoogleCalendar(s.google_service_account_file, s.google_calendar_id)
    except Exception:
        log.exception("Не вдалося підключити Google Calendar — перевірте файл ключа")
        return None
    return _calendar


def reset() -> None:
    global _calendar, _override
    _calendar, _override = None, False
