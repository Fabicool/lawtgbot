from datetime import datetime

from app.channels.telegram.sender import split_text
from app.core.conflicts import FUZZY_THRESHOLD, find_matches, name_similarity, normalize_name, normalize_phone
from app.core.masking import mask_pii
from app.core.statuses import CaseStatus, can_transition
from app.core.workdays import add_workdays
from app.db.models import Client, Party


def test_mask_pii():
    text = (
        "Паспорт АН 123456, ID-картка 001234567, РНОКПП 3012345678, тел. +380 67 123 45 67, "
        "картка 5375 4141 0000 1234, ЄДРПОУ 12345678, справа 757/12345/23, сума 15000 грн"
    )
    masked = mask_pii(text)
    for secret in ("123456,", "001234567", "3012345678", "123 45 67", "5375"):
        assert secret not in masked
    assert "12345678" in masked  # ЄДРПОУ компанії — публічний
    assert "757/12345/23" in masked
    assert "15000" in masked


def test_normalize():
    assert normalize_name("ТОВ «Ромашка»") == "ромашка"
    assert normalize_name("  Петренко   Олена ") == "петренко олена"
    assert normalize_phone("067 123 45 67") == "+380671234567"
    assert normalize_phone("+380671234567") == "+380671234567"


def test_similarity():
    assert name_similarity(normalize_name("Іванов Іван"), normalize_name("Иванов Иван")) >= FUZZY_THRESHOLD
    assert name_similarity(normalize_name("Іван Іванов"), normalize_name("Іванов Іван")) == 100
    assert (
        name_similarity(normalize_name("Шевченко Тарас"), normalize_name("Коваленко Тарас")) < FUZZY_THRESHOLD
    )
    assert (
        name_similarity(normalize_name("Петренко Олег"), normalize_name("Петренко Олег Васильович"))
        >= FUZZY_THRESHOLD
    )
    assert (
        name_similarity(normalize_name("Петренко Олена"), normalize_name("Петренко Олег")) < FUZZY_THRESHOLD
    )


def test_transitions():
    assert can_transition(CaseStatus.NEW, CaseStatus.VERIFIED)
    assert not can_transition(CaseStatus.NEW, CaseStatus.SENT)
    assert can_transition(CaseStatus.SURVEY, CaseStatus.CLOSED)


def test_workdays():
    friday = datetime(2026, 10, 23, 12, 0)
    assert add_workdays(friday, 2) == datetime(2026, 10, 27, 12, 0)  # вівторок


def test_split_text():
    text = ("абзац " * 100 + "\n") * 20
    parts = split_text(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


async def test_find_matches(sm):
    async with sm() as s:
        s.add(
            Client(
                full_name="Петренко Олена", name_norm=normalize_name("Петренко Олена"), phone="+380671112233"
            )
        )
        s.add(Party(name="ТОВ «Ромашка»", name_norm=normalize_name("ТОВ «Ромашка»")))
        await s.commit()
        m = await find_matches(s, "Олена Петренко")
        assert m and m[0].kind == "client"
        m = await find_matches(s, "Ромашка ТОВ")
        assert m and m[0].kind == "party" and m[0].reason == "exact_name"
        m = await find_matches(s, "Хтось Інший", phone="0671112233")
        assert m and m[0].reason == "phone"
        assert await find_matches(s, "Коваленко Тарас") == []
