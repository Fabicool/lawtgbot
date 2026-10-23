import re

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.channels.telegram.intake import IncomingEvent, IntakeFlow
from app.core import excel
from app.core.statuses import CaseStatus, ConflictStatus
from app.db.models import Case, Client, Draft, Lawyer, Message
from app.web.app import create_app
from app.web.auth import hash_password

SECRET = pyotp.random_base32()


async def fetch(_):
    return b""


@pytest.fixture
async def web(sm, sender):
    async with sm() as s:
        s.add(Lawyer(username="adv", password_hash=hash_password("password123"), totp_secret=SECRET))
        await s.commit()
    flow = IntakeFlow(sm, fetch)
    app = create_app(flow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.flow = flow
        yield client


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def login(client: AsyncClient) -> str:
    page = await client.get("/login")
    token = csrf_of(page.text)
    r = await client.post(
        "/login",
        data={"csrf": token, "username": "adv", "password": "password123", "code": pyotp.TOTP(SECRET).now()},
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = await client.get("/")
    assert home.status_code == 200
    return csrf_of(home.text)


async def test_requires_login(web):
    r = await web.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await web.get("/privacy")
    assert r.status_code == 200 and "захищаємо" in r.text


async def test_wrong_code_rejected(web):
    token = csrf_of((await web.get("/login")).text)
    r = await web.post(
        "/login", data={"csrf": token, "username": "adv", "password": "password123", "code": "000000"}
    )
    assert "Невірний" in r.text
    assert (await web.get("/")).status_code == 303


async def test_csrf_required(web):
    await login(web)
    r = await web.post("/settings", data={"lawyer_name": "X"})
    assert r.status_code == 403


async def make_case_in_review(flow: IntakeFlow) -> None:
    ev = lambda **kw: IncomingEvent(chat_id=5, user_id=5, **kw)  # noqa: E731
    for e in [
        ev(text="/start"),
        ev(callback="consent_yes"),
        ev(callback="urgent:none"),
        ev(text="Бондар Ірина"),
        ev(text="0931112233"),
        ev(callback="no_opponent"),
        ev(callback="cat:other"),
    ]:
        await flow.handle(e)
    for i in range(6):
        await flow.handle(ev(text=f"відповідь {i}"))
    await flow.handle(ev(callback="docs_done"))


async def test_full_lawyer_flow(web, sm, sender):
    token = await login(web)
    await make_case_in_review(web.flow)
    async with sm() as s:
        case = await s.scalar(select(Case))
    assert case.status == CaseStatus.IN_REVIEW

    for url in [
        "/",
        "/chats",
        "/proposals",
        "/clients",
        "/settings",
        f"/cases/{case.id}",
        f"/cases/{case.id}/messages",
        f"/clients/{case.client_id}",
    ]:
        r = await web.get(url)
        assert r.status_code == 200, url
    page = await web.get(f"/cases/{case.id}")
    assert "Бондар Ірина" in page.text and "відповідь 0" in page.text

    # Пряма відповідь адвоката → клієнт отримує, асистент замовкає
    r = await web.post(f"/cases/{case.id}/reply", data={"csrf": token, "text": "Добрий день, уточніть дату."})
    assert r.status_code == 303
    assert sender.sent[-1][1] == "Добрий день, уточніть дату."

    # Чернетка → затвердження → надіслано
    await web.post(f"/cases/{case.id}/draft", data={"csrf": token, "text": "Консультація", "action": "save"})
    r = await web.post(
        f"/cases/{case.id}/draft", data={"csrf": token, "text": "Консультація: ...", "action": "approve"}
    )
    assert r.status_code == 303
    assert sender.sent[-1][1].startswith("Консультація: ...")
    async with sm() as s:
        case = await s.get(Case, case.id)
        draft = await s.scalar(select(Draft))
        lawyer_msgs = (await s.scalars(select(Message).where(Message.author == "lawyer"))).all()
    assert case.status == CaseStatus.SENT and case.handoff == "lawyer"
    assert draft.state == "sent" and draft.sent_at
    assert len(lawyer_msgs) == 2

    # Нотатка не йде клієнту
    n = len(sender.sent)
    await web.post(f"/cases/{case.id}/note", data={"csrf": token, "text": "внутрішнє"})
    assert len(sender.sent) == n

    r = await web.get("/export/cases.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


async def test_conflict_resolution_resumes_survey(web, sm, sender):
    token = await login(web)
    async with sm() as s:
        s.add(Client(full_name="Сидоренко Андрій", name_norm="сидоренко андрій", source="import"))
        await s.commit()
    ev = lambda **kw: IncomingEvent(chat_id=9, user_id=9, **kw)  # noqa: E731
    for e in [
        ev(text="/start"),
        ev(callback="consent_yes"),
        ev(callback="urgent:none"),
        ev(text="Мельник Ольга"),
        ev(text="0991112233"),
        ev(text="Сидоренко Андрій"),
    ]:
        await web.flow.handle(e)
    async with sm() as s:
        case = await s.scalar(select(Case).where(Case.intake_step == "conflict_wait"))
    assert case is not None
    r = await web.post(f"/cases/{case.id}/conflict", data={"csrf": token, "decision": "clear"})
    assert r.status_code == 303
    async with sm() as s:
        case = await s.get(Case, case.id)
    assert case.conflict_status == ConflictStatus.CONFIRMED_CLEAR
    assert case.intake_step == "category" and case.status == CaseStatus.SURVEY


async def test_excel_import_export_roundtrip(web, sm):
    token = await login(web)
    data = excel.template_bytes()
    r = await web.post(
        "/clients/import", data={"csrf": token}, files={"file": ("t.xlsx", data, "application/octet-stream")}
    )
    assert r.status_code == 303
    async with sm() as s:
        clients = (await s.scalars(select(Client))).all()
    assert any(c.full_name == "Коваленко Марія Іванівна" for c in clients)
    exported = await web.get("/export/clients.xlsx")
    async with sm() as s:
        # Повторний імпорт експортованого файлу не створює дублікатів
        result = await excel.import_xlsx(s, exported.content)
    assert result.clients == 0 and result.skipped >= 1
