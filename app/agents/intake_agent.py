"""ШІ-частина Приймальника: терміновість, категорія й уточнення, читабельність документів."""

import re

from app.agents import llm, prompts
from app.agents.schemas import ClarifyResult, DocCheckResult, UrgencyResult
from app.core.masking import mask_pii

URGENT_KEYWORDS = [
    r"затрима",
    r"задержа",
    r"обшук",
    r"обыск",
    r"допит",
    r"допрос",
    r"підозр",
    r"подозр",
    r"повістк",
    r"повестк",
    r"засіданн",
    r"заседани",
    r"арешт",
    r"арест",
    r"виселя",
    r"выселя",
    r"завтра суд",
    r"сьогодні суд",
    r"строк.{0,20}(спливає|закінчується)",
    r"ДБР",
    r"СБУ",
    r"поліці",
]
_URGENT_RE = re.compile("|".join(URGENT_KEYWORDS), re.IGNORECASE)

# Вихідний фільтр: фрази, що схожі на правову оцінку. Такі питання не надсилаються клієнту.
_ADVICE_RE = re.compile(
    r"ви маєте право|вам слід|радимо|раджу|рекомендую|варто подати|шанси|виграєте|програєте|"
    r"згідно (зі )?статт|відповідно до статт|ст\.\s*\d|кодекс|закон україни|вартість послуг|коштуватиме",
    re.IGNORECASE,
)


def keyword_urgency(text: str) -> str:
    match = _URGENT_RE.search(text or "")
    return match.group(0) if match else ""


def looks_like_advice(text: str) -> bool:
    return bool(_ADVICE_RE.search(text))


async def check_urgency(text: str) -> tuple[bool, str]:
    """Спершу ключові слова (миттєво й безкоштовно), потім — ШІ, якщо доступний."""
    hit = keyword_urgency(text)
    if hit:
        return True, f"ключове слово: «{hit}»"
    result = await llm.parse(UrgencyResult, prompts.URGENCY, llm.wrap_client_data(mask_pii(text)))
    if result and result.urgent:
        return True, result.reason
    return False, ""


async def clarify(answers: dict[str, str], asked: list[str], max_questions: int = 3) -> ClarifyResult | None:
    if not llm.enabled():
        return None
    qa = "\n".join(f"Питання: {q}\nВідповідь: {mask_pii(a)}" for q, a in answers.items())
    already = "\n".join(f"- {q}" for q in asked)
    content = f"Уже поставлені питання:\n{already}\n\n{llm.wrap_client_data(qa)}"
    result = await llm.parse(ClarifyResult, prompts.INTAKE_CLARIFY, content)
    if result is None:
        return None
    safe = [q.strip() for q in result.questions if q.strip() and not looks_like_advice(q)]
    result.questions = safe[:max_questions]
    return result


async def check_document(data: bytes, mime: str) -> DocCheckResult | None:
    """Перевірка читабельності фото документа. PDF не перевіряємо (зазвичай це скан або оригінал)."""
    if not llm.enabled() or mime not in {"image/jpeg", "image/png", "image/webp"}:
        return None
    if len(data) > 4_500_000:
        return None
    content = [llm.image_block(data, mime), {"type": "text", "text": "Чи читабельний цей документ?"}]
    return await llm.parse(DocCheckResult, prompts.DOC_CHECK, content, max_tokens=2000)
