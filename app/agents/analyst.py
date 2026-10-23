"""Аналітик справи: розповідь клієнта + документи → картка справи для адвоката."""

import logging
from pathlib import Path

from app.agents import llm, prompts
from app.agents.schemas import CaseCard
from app.core.defaults import CATEGORIES
from app.core.masking import mask_pii
from app.db.models import Case

log = logging.getLogger(__name__)

MAX_DOC_BYTES = 4_500_000
MAX_DOCS = 8


def _intake_text(case: Case) -> str:
    lines = []
    for question, answer in (case.intake.get("answers") or {}).items():
        lines.append(f"Питання: {question}\nВідповідь: {mask_pii(answer)}")
    if case.intake.get("opponent"):
        lines.append(f"Друга сторона (зі слів клієнта): {case.intake['opponent']}")
    if case.urgency_reason:
        lines.append(f"Ознаки терміновості: {case.urgency_reason}")
    if case.intake.get("urgent_text"):
        lines.append(f"Про терміновість клієнт написав: {mask_pii(case.intake['urgent_text'])}")
    extra = case.intake.get("extra") or []
    if extra:
        lines.append("Додатково клієнт написав:\n" + "\n".join(f"- {mask_pii(t)}" for t in extra[-20:]))
    return "\n\n".join(lines)


def _doc_blocks(case: Case) -> list[dict]:
    blocks: list[dict] = []
    for doc in case.documents[:MAX_DOCS]:
        path = Path(doc.path)
        if not path.exists() or doc.size > MAX_DOC_BYTES:
            continue
        data = path.read_bytes()
        if doc.mime == "application/pdf":
            blocks.append(llm.pdf_block(data))
        elif doc.mime in {"image/jpeg", "image/png", "image/webp"}:
            blocks.append(llm.image_block(data, doc.mime))
    return blocks


def fallback_card(case: Case) -> dict:
    """Картка без ШІ: просто впорядковані відповіді анкети."""
    answers = case.intake.get("answers") or {}
    summary = "\n".join(f"{q} — {a}" for q, a in answers.items())
    first = next(iter(answers.values()), "").strip()
    title = (first[:77] + "…") if len(first) > 80 else first
    return {
        "title": case.title or title or f"Звернення #{case.id}",
        "summary": summary or "Клієнт ще не заповнив анкету.",
        "chronology": [],
        "parties": [{"name": case.intake.get("opponent", ""), "role": "друга сторона"}]
        if case.intake.get("opponent")
        else [],
        "amounts": [],
        "deadlines": [],
        "client_goal": "",
        "documents_provided": [d.filename for d in case.documents],
        "gaps": [],
        "category": case.category or "other",
        "complexity": "simple",
        "complexity_reason": "",
        "ai": False,
    }


async def build_card(case: Case) -> dict:
    """Повертає картку як dict (зберігається в Case.card). Без ШІ — спрощена картка."""
    if not llm.enabled():
        return fallback_card(case)
    content: list[dict] = _doc_blocks(case)
    category_hint = CATEGORIES.get(case.category, "")
    text = (
        f"Категорія, яку обрав клієнт: {category_hint or 'не вказано'}\n"
        f"Надіслані файли: {', '.join(d.filename for d in case.documents) or 'немає'}\n\n"
        + llm.wrap_client_data(_intake_text(case))
    )
    content.append({"type": "text", "text": text})
    card = await llm.parse(CaseCard, prompts.ANALYST, content, effort="medium", max_tokens=16000)
    if card is None:
        log.warning("Аналітик не відповів, використовую спрощену картку для справи %s", case.id)
        return fallback_card(case)
    data = card.model_dump()
    data["ai"] = True
    return data
