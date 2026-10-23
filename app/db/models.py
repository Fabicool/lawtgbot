"""Таблиці бази даних платформи (SQLite).

Персональні коди (РНОКПП/ЄДРПОУ) зберігаються лише зашифрованими (див. app.core.crypto).
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.statuses import CaseStatus, ConflictStatus


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Lawyer(Base):
    __tablename__ = "lawyers"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    name_norm: Mapped[str] = mapped_column(String(255), default="", index=True)
    is_company: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str] = mapped_column(String(32), default="", index=True)
    tax_id_enc: Mapped[str] = mapped_column(Text, default="")  # РНОКПП/ЄДРПОУ, зашифровано
    tax_id_hash: Mapped[str] = mapped_column(String(64), default="", index=True)  # для пошуку збігів
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    telegram_username: Mapped[str] = mapped_column(String(64), default="")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="bot")  # bot | import | manual
    has_contract: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cases: Mapped[list["Case"]] = relationship(back_populates="client", order_by="Case.id.desc()")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=CaseStatus.NEW, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    title: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    urgency_reason: Mapped[str] = mapped_column(Text, default="")
    is_complex: Mapped[bool] = mapped_column(Boolean, default=False)
    handoff: Mapped[str] = mapped_column(String(16), default="assistant")  # assistant | lawyer
    intake_step: Mapped[str] = mapped_column(String(32), default="")
    intake: Mapped[dict] = mapped_column(JSON, default=dict)  # відповіді опитування
    card: Mapped[dict] = mapped_column(JSON, default=dict)  # картка справи від Аналітика
    conflict_status: Mapped[str] = mapped_column(String(24), default=ConflictStatus.NOT_CHECKED)
    conflict_matches: Mapped[list] = mapped_column(JSON, default=list)
    response_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_client_activity: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    unread: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    client: Mapped[Client] = relationship(back_populates="cases")
    parties: Mapped[list["Party"]] = relationship(back_populates="case")
    messages: Mapped[list["Message"]] = relationship(back_populates="case", order_by="Message.id")
    documents: Mapped[list["Document"]] = relationship(back_populates="case", order_by="Document.id")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="case", order_by="Draft.id.desc()")

    @property
    def status_enum(self) -> CaseStatus:
        return CaseStatus(self.status)

    @property
    def conflict_enum(self) -> ConflictStatus:
        return ConflictStatus(self.conflict_status)


class Party(Base):
    """Інша сторона справи (опонент тощо). Використовується для перевірки конфліктів."""

    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    name_norm: Mapped[str] = mapped_column(String(255), index=True)
    is_company: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_id_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="opponent")
    related_client_name: Mapped[str] = mapped_column(String(255), default="")  # для імпорту
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case | None] = relationship(back_populates="parties")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # in | out | note
    author: Mapped[str] = mapped_column(String(16))  # client | assistant | lawyer | system
    text: Mapped[str] = mapped_column(Text, default="")
    masked_text: Mapped[str] = mapped_column(Text, default="")  # те, що бачить ШІ
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case] = relationship(back_populates="messages")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    readable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    check_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case] = relationship(back_populates="documents")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(16), default="draft")  # draft | sent
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    case: Mapped[Case] = relationship(back_populates="drafts")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))  # lawyer:<login> | bot | system
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
