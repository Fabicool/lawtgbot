"""Структури відповідей агентів."""

from typing import Literal

from pydantic import BaseModel, Field

CategoryKey = Literal[
    "family",
    "criminal",
    "labor",
    "housing",
    "civil",
    "inheritance",
    "administrative",
    "military",
    "business",
    "other",
]


class ChronoItem(BaseModel):
    date: str = Field(description="Дата або період як сказав клієнт; порожньо, якщо невідомо")
    event: str


class PartyItem(BaseModel):
    name: str
    role: str = Field(description="Роль: клієнт, друга сторона, свідок, орган тощо")


class DeadlineItem(BaseModel):
    date: str
    description: str
    urgent: bool = Field(
        description="True, якщо до строку 3 дні або менше чи строк неясний, але може спливати"
    )


class CaseCard(BaseModel):
    title: str = Field(description="Коротка назва справи, до 80 символів")
    summary: str = Field(description="Суть питання 3–6 реченнями, лише зі слів клієнта")
    chronology: list[ChronoItem]
    parties: list[PartyItem]
    amounts: list[str] = Field(description="Суми, згадані клієнтом, з контекстом")
    deadlines: list[DeadlineItem]
    client_goal: str
    documents_provided: list[str]
    gaps: list[str] = Field(description="Чого бракує у фактах, що адвокату варто уточнити")
    category: CategoryKey
    complexity: Literal["simple", "complex"]
    complexity_reason: str


class ClarifyResult(BaseModel):
    category: CategoryKey
    questions: list[str] = Field(description="0–3 уточнювальні питання; порожньо, якщо фактів достатньо")


class UrgencyResult(BaseModel):
    urgent: bool
    reason: str


class DocCheckResult(BaseModel):
    readable: bool
    note: str = Field(description="Коротко, що не так: розмито, обрізано, темно; порожньо, якщо все добре")
