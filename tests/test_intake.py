from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.channels.telegram.intake import IncomingEvent, IncomingFile, IntakeFlow
from app.core import crypto
from app.core.conflicts import normalize_name
from app.core.statuses import CaseStatus, ConflictStatus
from app.db.models import Case, Client, Document, Message

CHAT = 1001
PDF = b"%PDF-1.4 test"


async def fetch(file_id: str) -> bytes:
    return PDF


def ev(**kw) -> IncomingEvent:
    return IncomingEvent(chat_id=CHAT, user_id=CHAT, message_id=kw.pop("message_id", 1), **kw)


async def load_case(sm) -> Case:
    async with sm() as s:
        return await s.scalar(
            select(Case)
            .order_by(Case.id.desc())
            .options(selectinload(Case.client), selectinload(Case.documents))
        )


async def run_until_survey(flow: IntakeFlow, opponent: str = "ТОВ Ромашка"):
    await flow.handle(ev(text="/start"))
    await flow.handle(ev(callback="consent_yes"))
    await flow.handle(ev(callback="urgent:none"))
    await flow.handle(ev(text="Коваленко Марія Іванівна"))
    await flow.handle(ev(contact_phone="380501234567", contact_user_id=CHAT))
    await flow.handle(ev(text=opponent))


async def test_full_intake_without_ai(sm, sender):
    flow = IntakeFlow(sm, fetch)
    await run_until_survey(flow)
    case = await load_case(sm)
    assert case.status == CaseStatus.SURVEY
    assert case.conflict_status == ConflictStatus.CLEAR
    assert case.client.phone == "+380501234567"
    assert case.client.consent_at is not None

    await flow.handle(ev(callback="cat:labor"))
    case = await load_case(sm)
    questions = case.intake["questions"]
    assert len(questions) == 8  # 6 базових + 2 трудових
    for i, _ in enumerate(questions):
        await flow.handle(ev(text=f"Відповідь {i}"))
    case = await load_case(sm)
    assert case.intake_step == "documents"

    await flow.handle(ev(file=IncomingFile("f1", "наказ.pdf", "application/pdf", len(PDF))))
    await flow.handle(ev(callback="docs_done"))
    case = await load_case(sm)
    assert case.status == CaseStatus.IN_REVIEW
    assert case.response_due is not None
    assert case.card["ai"] is False
    assert "Відповідь 0" in case.card["summary"]
    assert len(case.documents) == 1
    assert any("протягом 2 робочих днів" in t for t in sender.texts)

    # Повторний /start не створює дубль звернення.
    await flow.handle(ev(text="/start"))
    async with sm() as s:
        assert len((await s.scalars(select(Case))).all()) == 1
    assert "уже в адвоката" in sender.texts[-1]

    # Після передачі адвокату асистент не відповідає по суті, а пересилає.
    await flow.handle(ev(text="А скільки це коштуватиме?"))
    case = await load_case(sm)
    assert case.unread is True
    assert "передано адвокату" in sender.texts[-1]


async def test_no_consent_closes_case(sm, sender):
    flow = IntakeFlow(sm, fetch)
    await flow.handle(ev(text="/start"))
    await flow.handle(ev(callback="consent_no"))
    case = await load_case(sm)
    assert case.status == CaseStatus.CLOSED


async def test_urgent_keyword(sm, sender):
    flow = IntakeFlow(sm, fetch)
    await flow.handle(ev(text="/start"))
    await flow.handle(ev(callback="consent_yes"))
    await flow.handle(ev(text="У мене зараз обшук у квартирі"))
    case = await load_case(sm)
    assert case.is_urgent
    assert case.intake_step == "name"


async def test_handoff(sm, sender):
    flow = IntakeFlow(sm, fetch)
    await flow.handle(ev(text="/start"))
    await flow.handle(ev(callback="consent_yes"))
    await flow.handle(ev(text="адвокат"))
    case = await load_case(sm)
    assert case.handoff == "lawyer"
    await flow.handle(ev(text="Мені потрібна допомога з розлученням"))
    async with sm() as s:
        msgs = (await s.scalars(select(Message).where(Message.case_id == case.id))).all()
    assert any(m.text == "Мені потрібна допомога з розлученням" for m in msgs)


async def test_conflict_opponent_is_client(sm, sender):
    async with sm() as s:
        s.add(Client(full_name="Петренко Олег", name_norm=normalize_name("Петренко Олег"), source="import"))
        await s.commit()
    flow = IntakeFlow(sm, fetch)
    await run_until_survey(flow, opponent="Петренко Олег Васильович")
    case = await load_case(sm)
    assert case.conflict_status == ConflictStatus.POSSIBLE
    assert case.intake_step == "conflict_wait"
    assert "на розгляді" in sender.texts[-1]


async def test_namesake_tax_id_hidden(sm, sender):
    async with sm() as s:
        s.add(
            Client(
                full_name="Коваленко Марія Іванівна",
                name_norm=normalize_name("Коваленко Марія Іванівна"),
                tax_id_enc=crypto.encrypt("1234567890"),
                tax_id_hash=crypto.code_hash("1234567890"),
                source="import",
            )
        )
        await s.commit()
    flow = IntakeFlow(sm, fetch)
    await flow.handle(ev(text="/start"))
    await flow.handle(ev(callback="consent_yes"))
    await flow.handle(ev(callback="urgent:none"))
    await flow.handle(ev(text="Коваленко Марія Іванівна"))
    await flow.handle(ev(text="0501234567"))
    case = await load_case(sm)
    assert case.intake_step == "tax_id"
    await flow.handle(ev(text="1234567890", message_id=77))
    assert (CHAT, 77) in sender.deleted
    case = await load_case(sm)
    assert case.intake["namesakes"][0]["same_person"] is True
    await flow.handle(ev(callback="no_opponent"))
    case = await load_case(sm)
    assert case.conflict_status == ConflictStatus.CLEAR
    assert case.intake["returning_client_id"]  # повторне звернення, не конфлікт
    async with sm() as s:
        texts = [m.text for m in (await s.scalars(select(Message))).all()]
    assert "1234567890" not in " ".join(texts)
    assert crypto.decrypt(case.client.tax_id_enc) == "1234567890"


async def test_files_saved_to_disk(sm, sender):
    flow = IntakeFlow(sm, fetch)
    await run_until_survey(flow)
    await flow.handle(ev(file=IncomingFile("f1", "скан.pdf", "application/pdf", 10)))
    async with sm() as s:
        doc = await s.scalar(select(Document))
    assert doc is not None
    from pathlib import Path

    assert Path(doc.path).read_bytes() == PDF


async def test_daily_limit_does_not_create_cases(sm, sender):
    from app.core.settings_store import set_value

    async with sm() as s:
        await set_value(s, "daily_limit", "1")
        await s.commit()
    flow = IntakeFlow(sm, fetch)
    await flow.handle(ev(text="/start"))
    other = IncomingEvent(chat_id=2002, user_id=2002, text="Добрий день")
    await flow.handle(other)
    await flow.handle(other)
    async with sm() as s:
        assert len((await s.scalars(select(Case))).all()) == 1
    assert "заповнений" in sender.texts[-1]
