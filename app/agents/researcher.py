"""Правовий дослідник: картка справи → правові джерела + чернетка консультації.

Пошук — вбудований web search Claude, обмежений офіційними сайтами (результати беруться з
пошукового індексу, тож геоблокування сайтів не заважає). Сторінки законів і рішень читає
інструмент read_page, який виконує сервер платформи (він в Україні, тож сайти відкриваються).
"""

import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.agents import llm
from app.config import get_settings
from app.core import links
from app.core.defaults import CATEGORIES

log = logging.getLogger(__name__)

MAX_TURNS = 14
MAX_PAGE_READS = 10


class SourceItem(BaseModel):
    kind: Literal["law", "court_decision", "legal_position"]
    title: str = Field(description="Назва акта або рішення")
    reference: str = Field(
        description="Точне посилання: «ст. 40 КЗпП України» або «постанова ВС від … у справі № …»"
    )
    url: str = Field(description="Повна адреса на zakon.rada.gov.ua, reyestr.court.gov.ua або court.gov.ua")
    case_number: str = Field(description="Номер справи для судового рішення, інакше порожньо")
    decision_date: str = Field(description="Дата рішення, інакше порожньо")
    relevance: str = Field(description="Чому це джерело стосується справи, 1–2 речення")


class ResearchResult(BaseModel):
    legal_issues: list[str] = Field(description="Правові питання, які треба вирішити")
    analysis: str = Field(description="Службова записка для адвоката: оцінка, ризики, варіанти дій, сумніви")
    draft_consultation: str = Field(description="Чернетка письмової консультації для клієнта простою мовою")
    sources: list[SourceItem]
    missing_info: list[str] = Field(description="Що ще треба з'ясувати у клієнта")
    recommend_meeting: bool = Field(description="Чи варто запропонувати особисту зустріч")
    document_draft: str = Field(
        description="Проєкт документа (заява, скарга, претензія), якщо потрібен; інакше порожньо"
    )


SYSTEM = """\
Ти — правовий дослідник у команді адвоката в Україні. За карткою справи знайди чинне
законодавство і правові позиції Верховного Суду та підготуй чернетку письмової консультації.
Остаточне рішення і підпис — за адвокатом: він перевірить кожне твоє посилання.

Як працювати:
1. Сформулюй правові питання справи.
2. Шукай через web_search лише на офіційних сайтах. Закони: zakon.rada.gov.ua/laws/show/…
   Рішення ВС: reyestr.court.gov.ua/Review/… Правові позиції: supreme.court.gov.ua, lpd.court.gov.ua.
3. Відкривай ключові сторінки інструментом read_page (з параметром find, напр. «Стаття 40»), щоб
   звірити текст норми чи рішення. Якщо сторінка не відкрилась, можна спиратися на результат пошуку,
   але напиши про це в analysis.
4. Перевір, що норма чинна (не втратила чинність, актуальна редакція), а позиція ВС не змінена
   пізнішою постановою Великої Палати.

Суворі правила:
- НІКОЛИ не вигадуй номери справ, дати, статті чи адреси. У sources — лише те, що ти реально
  знайшов пошуком або прочитав. Немає точного посилання — не включай джерело, а напиши в analysis.
- Кожне джерело — з повною адресою на дозволеному сайті; для рішень — номер справи і дата.
- Чернетка консультації: українською, простою мовою, без канцеляризмів. Структура: коротко про
  ситуацію → що передбачає закон (з посиланнями на статті) → можливі дії та їх ризики → що потрібно
  від клієнта → застереження, що консультацію надано на підставі отриманої від клієнта інформації.
- Не обіцяй результату, не називай вартість послуг, не вирішуй, чи братися за справу.
- Персональні коди в даних замасковано як *** — це нормально."""

READ_PAGE_TOOL = {
    "name": "read_page",
    "description": (
        "Відкрити сторінку офіційного правового сайту (zakon.rada.gov.ua, reyestr.court.gov.ua, "
        "court.gov.ua) і отримати її текст. Параметр find — фрагмент, біля якого показати текст "
        "(напр. «Стаття 40» або номер справи); порожній рядок — початок сторінки."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}, "find": {"type": "string"}},
        "required": ["url", "find"],
        "additionalProperties": False,
    },
}


def web_search_tool(model: str) -> dict:
    # Нова версія з фільтрацією працює на Opus/Sonnet 4.6+; для Haiku — базова.
    tool_type = "web_search_20250305" if "haiku" in model else "web_search_20260209"
    return {
        "type": tool_type,
        "name": "web_search",
        "max_uses": 8,
        "allowed_domains": list(links.ALLOWED_DOMAINS),
        "user_location": {"type": "approximate", "country": "UA", "timezone": "Europe/Kyiv"},
    }


def build_prompt(card: dict, category: str, previous: dict | None, comment: str) -> str:
    parts = [
        f"Категорія: {CATEGORIES.get(category, category or 'не визначено')}",
        "Картка справи (зі слів клієнта):",
        llm.wrap_client_data(
            json.dumps({k: v for k, v in card.items() if k != "ai"}, ensure_ascii=False, indent=1)
        ),
    ]
    if previous and comment:
        parts += [
            "Попередня чернетка, яку адвокат повернув на доопрацювання:",
            previous.get("draft_consultation", ""),
            f"Коментар адвоката (виконай його): {comment}",
        ]
    return "\n\n".join(parts)


async def _run_read_page(tool_input: dict, reads: int) -> str:
    if reads >= MAX_PAGE_READS:
        return "Ліміт відкриття сторінок вичерпано. Завершуй дослідження з тим, що вже знайдено."
    url = str(tool_input.get("url", ""))
    if not links.domain_allowed(url):
        return "Помилка: дозволено лише zakon.rada.gov.ua, reyestr.court.gov.ua, court.gov.ua."
    page = await links.fetch(url)
    if page.error or page.status != 200:
        reason = page.error or f"HTTP {page.status}"
        return f"Сторінка не відкрилась ({reason}). Спирайся на результати пошуку."
    return links.excerpt(page.text, str(tool_input.get("find", "")))


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _str_list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["law", "court_decision", "legal_position"]},
        "title": _str("Назва акта або рішення"),
        "reference": _str("Точне посилання: «ст. 40 КЗпП України» або «постанова ВС від … у справі № …»"),
        "url": _str("Повна адреса на zakon.rada.gov.ua, reyestr.court.gov.ua або court.gov.ua"),
        "case_number": _str("Номер справи для судового рішення, інакше порожньо"),
        "decision_date": _str("Дата рішення, інакше порожньо"),
        "relevance": _str("Чому джерело стосується справи, 1–2 речення"),
    },
    "required": ["kind", "title", "reference", "url", "case_number", "decision_date", "relevance"],
    "additionalProperties": False,
}

# Результат повертається через інструмент, а не через структурований формат відповіді:
# так він не конфліктує з цитатами, які додає вебпошук.
SUBMIT_TOOL = {
    "name": "submit_research",
    "description": "Здати результат дослідження. Викликай один раз, наприкінці, коли все перевірено.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "legal_issues": _str_list("Правові питання, які треба вирішити"),
            "analysis": _str("Службова записка для адвоката: оцінка, ризики, варіанти дій, сумніви"),
            "draft_consultation": _str("Чернетка письмової консультації для клієнта простою мовою"),
            "sources": {"type": "array", "items": _SOURCE_SCHEMA},
            "missing_info": _str_list("Що ще треба з'ясувати у клієнта"),
            "recommend_meeting": {
                "type": "boolean",
                "description": "Чи варто запропонувати особисту зустріч",
            },
            "document_draft": _str(
                "Проєкт документа (заява, скарга, претензія), якщо потрібен; інакше порожньо"
            ),
        },
        "required": [
            "legal_issues",
            "analysis",
            "draft_consultation",
            "sources",
            "missing_info",
            "recommend_meeting",
            "document_draft",
        ],
        "additionalProperties": False,
    },
}

FINISH_NUDGE = "Заверши дослідження: виклич інструмент submit_research з результатом."


async def research(
    card: dict, category: str, *, previous: dict | None = None, comment: str = ""
) -> ResearchResult | None:
    """Повертає результат або None (ШІ вимкнено / помилка)."""
    if not llm.enabled():
        return None
    model = get_settings().claude_model
    tools = [web_search_tool(model), READ_PAGE_TOOL, SUBMIT_TOOL]
    messages: list[dict] = [{"role": "user", "content": build_prompt(card, category, previous, comment)}]
    kwargs: dict = {
        "model": model,
        "max_tokens": 32000,
        "system": f"{SYSTEM}\n\nНаприкінці обов'язково виклич submit_research.\n\n{llm.DATA_GUARD}",
        "tools": tools,
        "output_config": {"effort": "high"},
        # Історія лише доповнюється — кешування зменшує вартість кожного наступного кроку.
        "cache_control": {"type": "ephemeral"},
    }
    if llm.supports_server_fallback(model):
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    reads = 0
    nudged = False
    try:
        for _ in range(MAX_TURNS):
            # Довгі відповіді (думання + чернетка) — лише потоком, інакше SDK відмовляє через таймаут.
            async with llm.client().beta.messages.stream(messages=messages, **kwargs) as stream:
                response = await stream.get_final_message()
            if response.stop_reason == "refusal":
                log.warning("Дослідник: відмова моделі")
                return None
            if response.stop_reason == "max_tokens":
                log.warning("Дослідник: відповідь обрізана")
                return None
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue  # серверний пошук ще триває — продовжуємо без нового повідомлення
            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            submit = next((b for b in tool_uses if b.name == "submit_research"), None)
            if submit is not None:
                try:
                    return ResearchResult.model_validate(submit.input)
                except ValueError:
                    log.warning("Дослідник: некоректний результат submit_research")
                    return None
            if tool_uses:
                results = []
                for block in tool_uses:
                    if block.name == "read_page":
                        content = await _run_read_page(block.input, reads)
                        reads += 1
                    else:
                        content = f"Невідомий інструмент {block.name}"
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
                messages.append({"role": "user", "content": results})
                continue
            # Модель завершила без submit_research — просимо здати результат один раз.
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": FINISH_NUDGE})
    except anthropic.APIConnectionError:
        log.warning("Дослідник: Claude API недоступний")
        return None
    except anthropic.APIStatusError as exc:
        log.error("Дослідник: помилка Claude API %s: %s", exc.status_code, exc.message)
        return None
    log.warning("Дослідник не завершив роботу")
    return None
