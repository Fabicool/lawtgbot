"""Маскування персональних даних до передачі тексту в ШІ (вимога ТЗ).

Оригінал зберігається в базі, ШІ бачить лише версію з «***».
"""

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Номер банківської картки: 16 цифр, можливо з пробілами/дефісами
    (re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"), "***"),
    # IBAN UA + 27 цифр
    (re.compile(r"\bUA\d{2}(?:\s?\d){25}\b", re.IGNORECASE), "UA***"),
    # Паспорт-книжечка: 2 літери (кирилиця/латиниця) + 6 цифр
    (re.compile(r"(?<![A-Za-zА-ЯІЇЄҐа-яіїєґ])[A-ZА-ЯІЇЄҐ]{2}\s?№?\s?\d{6}(?!\d)", re.IGNORECASE), "***"),
    # Український телефон (+380..., 380..., 0...) — ШІ він не потрібен
    (re.compile(r"(?<!\d)(?:\+?38[ -]?)?\(?0[ -]?\d{2}\)?[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"), "[телефон]"),
    # РНОКПП — 10 цифр
    (re.compile(r"(?<!\d)\d{10}(?!\d)"), "***"),
    # ID-картка — 9 цифр, ЄДРПОУ — 8 цифр навмисно НЕ маскуємо (код компанії публічний)
    (re.compile(r"(?<!\d)\d{9}(?!\d)"), "***"),
]


def mask_pii(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def contains_pii(text: str) -> bool:
    return mask_pii(text) != text
