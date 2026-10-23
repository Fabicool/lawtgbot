"""Перевірка конфлікту інтересів.

Точні збіги (нормалізований ПІБ, телефон, хеш коду) рахує код. Неточні збіги (одруківки,
транслітерація, «ТОВ» vs без форми власності) — rapidfuzz. Рішення завжди за адвокатом.
"""

import re
from dataclasses import asdict, dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Party

FUZZY_THRESHOLD = 90

_COMPANY_FORMS = (
    "товариство з обмеженою відповідальністю",
    "приватне підприємство",
    "публічне акціонерне товариство",
    "приватне акціонерне товариство",
    "акціонерне товариство",
    "фізична особа-підприємець",
    "фізична особа підприємець",
    "тов",
    "пп",
    "пат",
    "прат",
    "ат",
    "фоп",
    "llc",
    "ltd",
)

# Російські/латинські літери, які часто плутають з українськими
_CHAR_MAP = str.maketrans({"ё": "е", "ы": "и", "э": "е", "ъ": "", "'": "", "’": "", "ʼ": "", "`": ""})


def normalize_name(name: str) -> str:
    s = name.lower().translate(_CHAR_MAP)
    s = re.sub(r"[«»\"“”„.,()]", " ", s)
    for form in sorted(_COMPANY_FORMS, key=len, reverse=True):
        s = re.sub(rf"(?<!\w){re.escape(form)}(?!\w)", " ", s)
    return " ".join(s.split())


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 9:
        digits = "380" + digits
    return "+" + digits


# Для порівняння «рос./укр. написання» (Иванов vs Іванов) зводимо схожі літери до одної.
_LOOSE_MAP = str.maketrans({"і": "и", "ї": "и", "й": "и", "є": "е", "ґ": "г"})


def name_similarity(a: str, b: str) -> int:
    """Схожість ПІБ без урахування порядку слів (Іванов Іван = Іван Іванов)."""
    if not a or not b:
        return 0
    la, lb = a.translate(_LOOSE_MAP), b.translate(_LOOSE_MAP)
    score = max(fuzz.token_sort_ratio(a, b), fuzz.token_sort_ratio(la, lb))
    # «Петренко Олег» vs «Петренко Олег Васильович»: без по батькові. Лише якщо в обох ≥ 2 слів,
    # інакше одне прізвище збігалося б з усіма однофамільцями.
    if len(a.split()) >= 2 and len(b.split()) >= 2:
        score = max(score, fuzz.token_set_ratio(la, lb))
    return int(score)


@dataclass
class Match:
    kind: str  # client | party
    ref_id: int
    name: str
    reason: str  # exact_name | fuzzy_name | phone | tax_id
    score: int
    role: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


async def find_matches(
    session: AsyncSession,
    name: str,
    *,
    phone: str = "",
    tax_id_hash: str = "",
    exclude_client_id: int | None = None,
    exclude_case_id: int | None = None,
) -> list[Match]:
    """Шукає особу серед клієнтів та інших сторін усіх справ."""
    norm = normalize_name(name)
    phone = normalize_phone(phone) if phone else ""
    matches: list[Match] = []

    clients = (await session.scalars(select(Client))).all()
    for c in clients:
        if c.id == exclude_client_id:
            continue
        if tax_id_hash and c.tax_id_hash == tax_id_hash:
            matches.append(Match("client", c.id, c.full_name, "tax_id", 100, "клієнт"))
        elif phone and c.phone == phone:
            matches.append(Match("client", c.id, c.full_name, "phone", 100, "клієнт"))
        elif norm and c.name_norm == norm:
            matches.append(Match("client", c.id, c.full_name, "exact_name", 100, "клієнт"))
        elif norm and (score := name_similarity(norm, c.name_norm)) >= FUZZY_THRESHOLD:
            matches.append(Match("client", c.id, c.full_name, "fuzzy_name", score, "клієнт"))

    parties = (await session.scalars(select(Party))).all()
    for p in parties:
        if exclude_case_id is not None and p.case_id == exclude_case_id:
            continue
        role = p.role or "інша сторона"
        if tax_id_hash and p.tax_id_hash == tax_id_hash:
            matches.append(Match("party", p.id, p.name, "tax_id", 100, role))
        elif norm and p.name_norm == norm:
            matches.append(Match("party", p.id, p.name, "exact_name", 100, role))
        elif norm and (score := name_similarity(norm, p.name_norm)) >= FUZZY_THRESHOLD:
            matches.append(Match("party", p.id, p.name, "fuzzy_name", score, role))

    matches.sort(key=lambda m: -m.score)
    return matches
