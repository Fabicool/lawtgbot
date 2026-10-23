"""Статуси звернення (шлях клієнта з ТЗ) і результати перевірки конфліктів."""

from enum import StrEnum


class CaseStatus(StrEnum):
    NEW = "new"
    VERIFIED = "verified"
    CONFLICT_CHECK = "conflict_check"
    SURVEY = "survey"
    DOCUMENTS = "documents"
    IN_REVIEW = "in_review"
    PREPARING = "preparing"
    APPROVED = "approved"
    SENT = "sent"
    INACTIVE = "inactive"
    CLOSED = "closed"
    DECLINED = "declined"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS: dict[CaseStatus, str] = {
    CaseStatus.NEW: "Нове",
    CaseStatus.VERIFIED: "Верифіковано",
    CaseStatus.CONFLICT_CHECK: "Перевірка конфлікту",
    CaseStatus.SURVEY: "Опитування",
    CaseStatus.DOCUMENTS: "Документи",
    CaseStatus.IN_REVIEW: "На розгляді",
    CaseStatus.PREPARING: "Готується відповідь",
    CaseStatus.APPROVED: "Затверджено",
    CaseStatus.SENT: "Консультацію надіслано",
    CaseStatus.INACTIVE: "Неактивне",
    CaseStatus.CLOSED: "Закрито",
    CaseStatus.DECLINED: "Відмовлено",
}

# Колонки дашборду: що чекає на дію адвоката — ліворуч.
DASHBOARD_COLUMNS: list[CaseStatus] = [
    CaseStatus.IN_REVIEW,
    CaseStatus.PREPARING,
    CaseStatus.APPROVED,
    CaseStatus.NEW,
    CaseStatus.VERIFIED,
    CaseStatus.CONFLICT_CHECK,
    CaseStatus.SURVEY,
    CaseStatus.DOCUMENTS,
    CaseStatus.SENT,
]

# Дозволені переходи. Адвокат може вручну закрити/відмовити з будь-якого статусу.
TRANSITIONS: dict[CaseStatus, set[CaseStatus]] = {
    CaseStatus.NEW: {CaseStatus.VERIFIED},
    CaseStatus.VERIFIED: {CaseStatus.CONFLICT_CHECK},
    CaseStatus.CONFLICT_CHECK: {CaseStatus.SURVEY},
    CaseStatus.SURVEY: {CaseStatus.DOCUMENTS, CaseStatus.IN_REVIEW},
    CaseStatus.DOCUMENTS: {CaseStatus.IN_REVIEW},
    CaseStatus.IN_REVIEW: {CaseStatus.PREPARING, CaseStatus.APPROVED},
    CaseStatus.PREPARING: {CaseStatus.APPROVED, CaseStatus.IN_REVIEW},
    CaseStatus.APPROVED: {CaseStatus.SENT},
    CaseStatus.SENT: {CaseStatus.PREPARING, CaseStatus.IN_REVIEW},
    CaseStatus.INACTIVE: {CaseStatus.IN_REVIEW, CaseStatus.SURVEY},
    CaseStatus.CLOSED: {CaseStatus.IN_REVIEW},
    CaseStatus.DECLINED: set(),
}
ALWAYS_ALLOWED = {CaseStatus.CLOSED, CaseStatus.DECLINED, CaseStatus.INACTIVE}

# Статуси, у яких клієнт ще проходить опитування з асистентом.
INTAKE_STATUSES = {
    CaseStatus.NEW,
    CaseStatus.VERIFIED,
    CaseStatus.CONFLICT_CHECK,
    CaseStatus.SURVEY,
    CaseStatus.DOCUMENTS,
}
OPEN_STATUSES = INTAKE_STATUSES | {
    CaseStatus.IN_REVIEW,
    CaseStatus.PREPARING,
    CaseStatus.APPROVED,
    CaseStatus.SENT,
}


def can_transition(src: CaseStatus, dst: CaseStatus) -> bool:
    return dst in ALWAYS_ALLOWED or dst in TRANSITIONS.get(src, set())


class ConflictStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    CLEAR = "clear"
    POSSIBLE = "possible"
    CONFIRMED_CLEAR = "confirmed_clear"  # адвокат перевірив і дозволив
    CONFLICT = "conflict"  # адвокат підтвердив конфлікт

    @property
    def label(self) -> str:
        return {
            "not_checked": "Не перевірено",
            "clear": "Конфлікту немає",
            "possible": "Можливий конфлікт",
            "confirmed_clear": "Перевірено адвокатом: конфлікту немає",
            "conflict": "Конфлікт підтверджено",
        }[self.value]
