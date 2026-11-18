"""Відповіді на уточнення клієнта після консультації — лише в межах затвердженого адвокатом тексту.

Модель зобов'язана навести дослівну цитату з консультації; якщо цитати в тексті немає,
відповідь не надсилається, а питання йде адвокату.
"""

import re

from pydantic import BaseModel, Field

from app.agents import llm
from app.core.masking import mask_pii

SYSTEM = """\
Ти — асистент адвоката. Клієнт уже отримав письмову консультацію, затверджену адвокатом.
Відповідай на його запитання ЛИШЕ тим, що прямо написано в консультації: поясни простіше,
перекажи потрібне місце.

Став answerable=false, якщо запитання потребує нової правової оцінки, нових фактів, прогнозу,
вартості, порівняння варіантів, яких немає в консультації, або ти не впевнений.
У quote — дослівний фрагмент консультації (від 20 символів), на якому ґрунтується відповідь.
Відповідь українською, коротко, 1–4 речення."""


class FollowupAnswer(BaseModel):
    answerable: bool
    answer: str
    quote: str = Field(description="Дослівна цитата з консультації")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_grounded(quote: str, consultation: str) -> bool:
    q = _norm(quote).strip("«»\"'.… ")
    return len(q) >= 20 and q in _norm(consultation)


async def answer(consultation: str, question: str) -> str | None:
    """Текст відповіді або None (питання — до адвоката)."""
    if not llm.enabled() or not consultation.strip():
        return None
    content = (
        f"Затверджена консультація:\n<consultation>\n{consultation}\n</consultation>\n\n"
        f"Запитання клієнта:\n{llm.wrap_client_data(mask_pii(question))}"
    )
    result = await llm.parse(FollowupAnswer, SYSTEM, content, max_tokens=4000)
    if result is None or not result.answerable or not result.answer.strip():
        return None
    if not quote_is_grounded(result.quote, consultation):
        return None
    return result.answer.strip()
