"""Етап 2: Дослідник, перевірка посилань, відповіді після консультації, зустрічі, нагадування."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.agents import followup, llm, researcher
from app.agents.researcher import ResearchResult, SourceItem
from app.channels.telegram.intake import IncomingEvent, IntakeFlow
from app.config import get_settings
from app.core import jobs, links, meetings, research_service, scheduler
from app.core.settings_store import set_value
from app.core.statuses import CaseStatus
from app.db import session as db_session
from app.db.models import Case, Client, Draft, Meeting, Source, utcnow
from app.integrations import google_calendar
from tests.test_agents import enable_ai
from tests.test_web import csrf_of, login, make_case_in_review

DECISION_URL = "https://reyestr.court.gov.ua/Review/111"
LAW_URL = "https://zakon.rada.gov.ua/laws/show/322-08"


def fake_sites(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == DECISION_URL:
        body = "<html><body>" + "Постанова Верховного Суду у справі № 757/12345/23 " * 50 + "</body></html>"
        return httpx.Response(200, html=body)
    if url == LAW_URL:
        return httpx.Response(200, html="<html><body><h1>Кодекс законів про працю</h1>" + "Стаття 40. " * 100)
    if "zakon.rada.gov.ua/laws/show/missing" in url:
        return httpx.Response(404, html="Not found")
    raise httpx.ConnectError("reset by peer")


@pytest.fixture(autouse=True)
def mocked_sites():
    links.transport = httpx.MockTransport(fake_sites)
    google_calendar.set_calendar(None)
    yield
    links.transport = None
    google_calendar.reset()


# ---------------- міграція ----------------


async def test_migration_adds_new_columns(sm):
    async with db_session._engine.begin() as conn:
        await conn.execute(text("ALTER TABLE cases DROP COLUMN research"))
        await conn.execute(text("ALTER TABLE drafts DROP COLUMN author"))
    await db_session.create_all()
    async with sm() as s:
        s.add(Client(full_name="Тест"))
        await s.flush()
        s.add(Case(client_id=1))
        await s.commit()
        case = await s.scalar(select(Case))
        assert case.research == {}


# ---------------- перевірка посилань ----------------


async def test_check_link_statuses():
    ok = await links.check_link(DECISION_URL, "757/12345/23")
    assert ok.status == "ok"
    wrong = await links.check_link(DECISION_URL, "910/1/24")
    assert wrong.status == "not_found"
    assert (await links.check_link("https://zakon.rada.gov.ua/laws/show/missing")).status == "not_found"
    assert (await links.check_link(LAW_URL)).status == "ok"
    assert (await links.check_link("https://reyestr.court.gov.ua/Review/999")).status == "unreachable"
    assert (await links.check_link("https://example.com/law")).status == "bad_domain"
    assert (await links.check_link("https://evil.com/?q=court.gov.ua")).status == "bad_domain"


def test_domain_allowed():
    assert links.domain_allowed("https://supreme.court.gov.ua/supreme/")
    assert links.domain_allowed("https://lpd.court.gov.ua/")
    assert not links.domain_allowed("https://court.gov.ua.evil.com/")
    assert not links.domain_allowed("ftp://zakon.rada.gov.ua/")


# ---------------- Дослідник ----------------

RESULT = ResearchResult(
    legal_issues=["Чи законне звільнення без попередження"],
    analysis="Звільнення з порушенням ст. 40 КЗпП.",
    draft_consultation="Шановна клієнтко! Відповідно до статті 40 КЗпП…",
    sources=[
        SourceItem(
            kind="law",
            title="КЗпП",
            reference="ст. 40 КЗпП України",
            url=LAW_URL,
            case_number="",
            decision_date="",
            relevance="Підстави звільнення",
        ),
        SourceItem(
            kind="court_decision",
            title="Постанова ВС",
            reference="справа № 757/12345/23",
            url=DECISION_URL,
            case_number="757/12345/23",
            decision_date="12.05.2024",
            relevance="Схожа справа",
        ),
    ],
    missing_info=["Дата наказу"],
    recommend_meeting=True,
    document_draft="",
)


class ResearchMessages:
    """1-й виклик: Claude відкриває сторінку; 2-й: завершує без результату; 3-й: здає результат."""

    def __init__(self):
        self.calls = []

    def stream(self, **kwargs):
        outer = self

        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return await outer.create(**kwargs)

        return _Stream()

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        n = len(self.calls)
        if n == 1:
            block = SimpleNamespace(
                type="tool_use", id="tu_1", name="read_page", input={"url": LAW_URL, "find": "Стаття 40"}
            )
            return SimpleNamespace(stop_reason="tool_use", content=[block])
        if n == 2:
            return SimpleNamespace(
                stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Готово")]
            )
        block = SimpleNamespace(type="tool_use", id="tu_2", name="submit_research", input=RESULT.model_dump())
        return SimpleNamespace(stop_reason="tool_use", content=[block])


async def test_researcher_loop_reads_pages(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    fake = ResearchMessages()
    llm.set_client(SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake))
    result = await researcher.research({"summary": "Звільнили"}, "labor")
    assert result == RESULT
    assert len(fake.calls) == 3  # без submit_research модель попросили здати результат
    assert fake.calls[2]["messages"][-2]["content"] == researcher.FINISH_NUDGE
    # Список повідомлень спільний для всіх викликів — шукаємо в ньому результат інструмента.
    user_turns = [
        m for m in fake.calls[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
    ]
    tool_result = user_turns[0]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1" and "Стаття 40" in tool_result["content"]
    tools = fake.calls[0]["tools"]
    assert tools[0]["type"] == "web_search_20260209" and "zakon.rada.gov.ua" in tools[0]["allowed_domains"]
    assert {t.get("name") for t in tools} == {"web_search", "read_page", "submit_research"}
    assert "output_format" not in fake.calls[0]
    assert fake.calls[0]["fallbacks"] == "default"


async def test_research_service_creates_sources_and_draft(monkeypatch, sm, sender):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()

    async def fake_research(card, category, previous=None, comment=""):
        return RESULT

    monkeypatch.setattr(researcher, "research", fake_research)
    async with sm() as s:
        s.add(Client(full_name="К"))
        await s.flush()
        s.add(Case(client_id=1, card={"summary": "x"}, status=CaseStatus.IN_REVIEW))
        await s.commit()
    assert await research_service.start(sm, 1)
    await jobs.wait_all()
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.sources), selectinload(Case.drafts)))
    assert case.research["state"] == "done"
    assert [src.check_status for src in case.sources] == ["ok", "ok"]
    assert case.drafts[0].author == "researcher" and "статті 40" in case.drafts[0].text


async def test_approve_requires_confirmed_sources(web, sm, sender):
    token = await login(web)
    await make_case_in_review(web.flow)
    async with sm() as s:
        s.add(Source(case_id=1, kind="law", reference="ст. 40 КЗпП", url=LAW_URL, check_status="ok"))
        await s.commit()
    page = await web.get("/cases/1")
    assert "ст. 40 КЗпП" in page.text
    sent_before = len(sender.sent)
    await web.post("/cases/1/draft", data={"csrf": token, "text": "Консультація", "action": "approve"})
    assert len(sender.sent) == sent_before  # не надіслано
    async with sm() as s:
        assert (await s.get(Case, 1)).status != CaseStatus.SENT
    await web.post(
        "/cases/1/draft", data={"csrf": token, "text": "Консультація", "action": "approve", "confirm_1": "on"}
    )
    assert sender.sent[-1][1].startswith("Консультація")
    async with sm() as s:
        assert (await s.get(Case, 1)).status == CaseStatus.SENT


async def test_exclude_source_allows_approve(web, sm, sender):
    token = await login(web)
    await make_case_in_review(web.flow)
    async with sm() as s:
        s.add(Source(case_id=1, kind="law", reference="сумнівне", url=LAW_URL))
        await s.commit()
    await web.post("/cases/1/draft", data={"csrf": token, "text": "Текст", "action": "exclude:1"})
    await web.post("/cases/1/draft", data={"csrf": token, "text": "Текст", "action": "approve"})
    async with sm() as s:
        assert (await s.get(Case, 1)).status == CaseStatus.SENT


# ---------------- відповіді після консультації ----------------

CONSULTATION = "Роботодавець мав попередити вас про звільнення щонайменше за два місяці. Це вимога закону."


def test_quote_grounding():
    assert followup.quote_is_grounded("попередити вас про звільнення щонайменше за два місяці", CONSULTATION)
    assert not followup.quote_is_grounded("ви виграєте справу в суді", CONSULTATION)
    assert not followup.quote_is_grounded("два", CONSULTATION)


async def sent_case(sm, flow, sender):
    ev = lambda **kw: IncomingEvent(chat_id=8, user_id=8, **kw)  # noqa: E731
    for e in [
        ev(text="/start"),
        ev(callback="consent_yes"),
        ev(callback="urgent:none"),
        ev(text="Лисенко Анна"),
        ev(text="0671231231"),
        ev(callback="no_opponent"),
        ev(callback="cat:other"),
    ]:
        await flow.handle(e)
    for i in range(6):
        await flow.handle(ev(text=f"відповідь {i}"))
    await flow.handle(ev(callback="docs_done"))
    async with sm() as s:
        case = await s.scalar(select(Case))
        case.status = CaseStatus.SENT
        s.add(Draft(case_id=case.id, text=CONSULTATION, state="sent", sent_at=utcnow()))
        await s.commit()
    return ev


async def test_followup_answers_within_consultation(monkeypatch, sm, sender):
    flow = IntakeFlow(sm, lambda _: b"")
    ev = await sent_case(sm, flow, sender)
    enable_ai(
        monkeypatch,
        {
            followup.FollowupAnswer: followup.FollowupAnswer(
                answerable=True,
                answer="За два місяці до звільнення.",
                quote="попередити вас про звільнення щонайменше за два місяці",
            )
        },
    )
    await flow.handle(ev(text="За скільки мали попередити?"))
    assert sender.texts[-1].startswith("За два місяці до звільнення.")


async def test_followup_ungrounded_goes_to_lawyer(monkeypatch, sm, sender):
    flow = IntakeFlow(sm, lambda _: b"")
    ev = await sent_case(sm, flow, sender)
    enable_ai(
        monkeypatch,
        {
            followup.FollowupAnswer: followup.FollowupAnswer(
                answerable=True, answer="Ви точно виграєте.", quote="ви точно виграєте справу"
            )
        },
    )
    await flow.handle(ev(text="Я виграю?"))
    assert "передав" in sender.texts[-1]
    async with sm() as s:
        assert (await s.get(Case, 1)).unread


# ---------------- зустрічі ----------------


class FakeCalendar:
    def __init__(self, busy=None):
        self._busy = busy or []
        self.created = []
        self.deleted = []

    async def busy(self, start, end):
        return self._busy

    async def create_event(self, start, end, summary, description, location):
        self.created.append((start, summary, location))
        return f"ev{len(self.created)}"

    async def delete_event(self, event_id):
        self.deleted.append(event_id)


async def setup_meetings(sm):
    async with sm() as s:
        await set_value(s, "office_address", "Київ, вул. Хрещатик, 1")
        await set_value(s, "meeting_days", "1,2,3,4,5")
        await s.commit()


async def test_free_slots_skip_busy_and_weekends(sm):
    await setup_meetings(sm)
    monday_9_kyiv_utc = datetime(2026, 11, 23, 7, 0)  # пн 09:00 Київ
    busy_start = meetings.to_utc(datetime(2026, 11, 24, 10, 0))  # вт 10:00 зайнято в Google
    google_calendar.set_calendar(FakeCalendar(busy=[(busy_start, busy_start + timedelta(hours=1))]))
    async with sm() as s:
        slots = await meetings.free_slots(s, now=monday_9_kyiv_utc)
    kyiv = [meetings.to_kyiv(x) for x in slots]
    assert len(slots) == 3
    assert len({k.date() for k in kyiv}) == 3  # різні дні
    assert all(k.weekday() < 5 for k in kyiv)
    assert all(slot >= monday_9_kyiv_utc + timedelta(hours=18) for slot in slots)
    assert busy_start not in slots


async def test_offer_and_book_meeting(sm, sender):
    await setup_meetings(sm)
    cal = FakeCalendar()
    google_calendar.set_calendar(cal)
    flow = IntakeFlow(sm, lambda _: b"")
    ev = await sent_case(sm, flow, sender)
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.client)))
        await meetings.offer(s, case, case.client)
        await s.commit()
    keyboard = sender.sent[-1][2]
    assert len(keyboard.rows) == 4 and keyboard.rows[-1][0].data == "meet:none"
    await flow.handle(ev(callback="meet:1"))
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.meetings)))
    assert case.status == CaseStatus.MEETING
    assert case.meetings[0].google_event_id == "ev1"
    assert "Хрещатик" in sender.texts[-1] and "google.com/maps" in sender.texts[-1]
    assert cal.created[0][2] == "Київ, вул. Хрещатик, 1"


async def test_taken_slot_gets_new_offer(sm, sender):
    await setup_meetings(sm)
    flow = IntakeFlow(sm, lambda _: b"")
    ev = await sent_case(sm, flow, sender)
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.client)))
        slots = await meetings.offer(s, case, case.client)
        s.add(Meeting(kind="block", starts_at=slots[0], ends_at=slots[0] + timedelta(hours=1)))
        await s.commit()
    await flow.handle(ev(callback="meet:0"))
    assert any("щойно зайняли" in t for t in sender.texts)
    async with sm() as s:
        assert (await s.scalar(select(Case))).status == CaseStatus.SENT


async def test_cancel_meeting_via_web(web, sm, sender):
    token = await login(web)
    await setup_meetings(sm)
    cal = FakeCalendar()
    google_calendar.set_calendar(cal)
    ev = await sent_case(sm, web.flow, sender)
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.client)))
        await meetings.offer(s, case, case.client)
        await s.commit()
    await web.flow.handle(ev(callback="meet:0"))
    page = await web.get("/meetings")
    assert page.status_code == 200 and "Лисенко Анна" in page.text
    await web.post("/meetings/1/cancel", data={"csrf": token})
    async with sm() as s:
        assert (await s.get(Meeting, 1)).status == "cancelled"
        assert (await s.get(Case, 1)).status == CaseStatus.SENT
    assert cal.deleted == ["ev1"] and "скасовано" in sender.texts[-1]


async def test_offer_meeting_from_platform(web, sm, sender):
    token = await login(web)
    await make_case_in_review(web.flow)
    r = await web.post("/cases/1/meeting/offer", data={"csrf": token, "fmt": "office"})
    assert r.status_code == 303
    page = await web.get("/cases/1")
    assert "адресу офісу" in page.text  # адреса ще не вказана — помилка
    await setup_meetings(sm)
    await web.post("/cases/1/meeting/offer", data={"csrf": csrf_of(page.text), "fmt": "office"})
    assert sender.sent[-1][2] is not None and sender.sent[-1][2].rows[0][0].data == "meet:0"


# ---------------- планувальник ----------------


async def test_meeting_reminders(sm, sender):
    flow = IntakeFlow(sm, lambda _: b"")
    await sent_case(sm, flow, sender)
    start = utcnow() + timedelta(days=3)
    async with sm() as s:
        s.add(Meeting(case_id=1, starts_at=start, ends_at=start + timedelta(hours=1), created_at=utcnow()))
        await s.commit()
    await scheduler.tick(sm, now=start - timedelta(hours=23))
    assert "завтра" in sender.texts[-1]
    n = len(sender.sent)
    await scheduler.tick(sm, now=start - timedelta(hours=22))
    assert len(sender.sent) == n  # повторно не надсилає
    await scheduler.tick(sm, now=start - timedelta(hours=1, minutes=30))
    assert "через 2 години" in sender.texts[-1]
    await scheduler.tick(sm, now=start + timedelta(hours=2))
    async with sm() as s:
        assert (await s.get(Meeting, 1)).status == "done"


async def test_client_gone_nudges_then_inactive_then_reactivates(sm, sender):
    flow = IntakeFlow(sm, lambda _: b"")
    ev = lambda **kw: IncomingEvent(chat_id=4, user_id=4, **kw)  # noqa: E731
    for e in [ev(text="/start"), ev(callback="consent_yes"), ev(callback="urgent:none")]:
        await flow.handle(e)
    async with sm() as s:
        last = (await s.get(Case, 1)).last_client_activity
    await scheduler.tick(sm, flow, now=last + timedelta(days=1))
    assert "Нагадую" not in sender.texts[-1]
    await scheduler.tick(sm, flow, now=last + timedelta(days=2, hours=1))
    assert any("Нагадую" in t for t in sender.texts[-3:])
    assert "прізвище" in sender.texts[-1]  # повторено поточне питання
    await scheduler.tick(sm, flow, now=last + timedelta(days=5, hours=1))
    assert "досі не завершене" in sender.texts[-1]
    await scheduler.tick(sm, flow, now=last + timedelta(days=7, hours=1))
    async with sm() as s:
        assert (await s.get(Case, 1)).status == CaseStatus.INACTIVE
    await flow.handle(ev(text="Петренко Іван"))
    async with sm() as s:
        case = await s.get(Case, 1)
        assert case.status == CaseStatus.NEW and case.intake_step == "phone"
        assert len((await s.scalars(select(Case))).all()) == 1


async def test_due_warning_email_once(monkeypatch, sm, sender):
    sent = []

    async def fake_notify(event, case_id, extra=""):
        sent.append(event)
        return True

    monkeypatch.setattr(scheduler.notify, "notify_lawyer", fake_notify)
    flow = IntakeFlow(sm, lambda _: b"")
    await sent_case(sm, flow, sender)
    async with sm() as s:
        case = await s.get(Case, 1)
        case.status = CaseStatus.IN_REVIEW
        case.response_due = utcnow() + timedelta(hours=10)
        await s.commit()
    await scheduler.tick(sm)
    await scheduler.tick(sm)
    assert sent.count("due_soon") == 1
