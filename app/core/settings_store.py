"""Налаштування платформи: значення з бази перекривають значення за замовчуванням."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import defaults
from app.db.models import Setting


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def default_value(key: str) -> str:
    if key in defaults.SETTINGS_DEFAULTS:
        return defaults.SETTINGS_DEFAULTS[key]
    if key.startswith("tpl."):
        return defaults.TEMPLATES.get(key[4:], "")
    if key == "base_questions":
        return "\n".join(defaults.BASE_QUESTIONS)
    if key.startswith("questions."):
        return "\n".join(defaults.CATEGORY_QUESTIONS.get(key[10:], []))
    if key.startswith("documents."):
        return "\n".join(defaults.DOCUMENTS.get(key[10:], []))
    return ""


async def get_value(session: AsyncSession, key: str) -> str:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    return default_value(key)


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


async def get_int(session: AsyncSession, key: str, fallback: int = 0) -> int:
    try:
        return int((await get_value(session, key)).strip())
    except ValueError:
        return fallback


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


async def survey_questions(session: AsyncSession, category: str) -> list[str]:
    base = _lines(await get_value(session, "base_questions"))
    extra = _lines(await get_value(session, f"questions.{category}")) if category else []
    return base + extra


async def document_list(session: AsyncSession, category: str) -> list[str]:
    return _lines(await get_value(session, f"documents.{category or 'other'}"))


async def template(session: AsyncSession, tpl_name: str, /, **kwargs: object) -> str:
    text = await get_value(session, f"tpl.{tpl_name}")
    lawyer_name = await get_value(session, "lawyer_name")
    lawyer_phone = await get_value(session, "lawyer_phone")
    values = _SafeDict(
        lawyer_name=lawyer_name or "",
        lawyer_phone=lawyer_phone or "(телефон буде вказано пізніше)",
        privacy_url=f"{get_settings().base_url.rstrip('/')}/privacy",
    )
    values.update({k: str(v) for k, v in kwargs.items()})
    text = text.format_map(values)
    return text.replace("адвоката .", "адвоката.").replace("  ", " ")
