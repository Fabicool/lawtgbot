from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents import intake_agent, llm
from app.agents.schemas import CaseCard, ClarifyResult, UrgencyResult
from app.channels.telegram.intake import IncomingEvent, IntakeFlow
from app.config import get_settings
from app.db.models import Case


class FakeMessages:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["output_format"]
        value = self.responses.get(model)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=value)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=self.messages)


def enable_ai(monkeypatch, responses):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    fake = FakeClient(responses)
    llm.set_client(fake)
    return fake


CARD = CaseCard(
    title="Звільнення без попередження",
    summary="Клієнтку звільнили.",
    chronology=[],
    parties=[],
    amounts=[],
    deadlines=[],
    client_goal="Поновитися",
    documents_provided=[],
    gaps=["дата наказу"],
    category="labor",
    complexity="complex",
    complexity_reason="кілька епізодів",
)


async def test_clarify_filters_advice(monkeypatch):
    enable_ai(
        monkeypatch,
        {
            ClarifyResult: ClarifyResult(
                category="labor",
                questions=["Коли ви отримали наказ?", "Ви маєте право на компенсацію, чи знали ви?"],
            )
        },
    )
    result = await intake_agent.clarify({"Що сталося?": "Звільнили"}, ["Що сталося?"])
    assert result.questions == ["Коли ви отримали наказ?"]


async def test_pii_never_sent_to_ai(monkeypatch):
    fake = enable_ai(monkeypatch, {UrgencyResult: UrgencyResult(urgent=False, reason="")})
    await intake_agent.check_urgency("Мій РНОКПП 3012345678, паспорт АН 123456, просто питання про договір")
    sent = str(fake.messages.calls[0]["messages"])
    assert "3012345678" not in sent and "123456" not in sent
    assert "<client_data>" in sent
    call = fake.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"


async def test_complex_case_gets_7_days(monkeypatch, sm, sender):
    enable_ai(
        monkeypatch,
        {
            UrgencyResult: UrgencyResult(urgent=False, reason=""),
            ClarifyResult: ClarifyResult(category="labor", questions=[]),
            CaseCard: CARD,
        },
    )
    flow = IntakeFlow(sm, lambda _: b"")
    ev = lambda **kw: IncomingEvent(chat_id=3, user_id=3, **kw)  # noqa: E731
    for e in [
        ev(text="/start"),
        ev(callback="consent_yes"),
        ev(callback="urgent:none"),
        ev(text="Гончар Світлана"),
        ev(text="0631112233"),
        ev(callback="no_opponent"),
        ev(callback="cat:labor"),
    ]:
        await flow.handle(e)
    for i in range(8):
        await flow.handle(ev(text=f"відповідь {i}"))
    await flow.handle(ev(callback="docs_done"))
    async with sm() as s:
        case = await s.scalar(select(Case).options(selectinload(Case.client)))
    assert case.is_complex and case.card["ai"] is True
    assert case.title == "Звільнення без попередження"
    assert any("протягом 7 робочих днів" in t for t in sender.texts)
