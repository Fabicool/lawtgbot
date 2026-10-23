"""Єдиний модуль повідомлень.

Усе листування з клієнтами проходить тут і зберігається в базі платформи, незалежно від
месенджера. Канал (зараз лише Telegram) реєструє свій «відправник»; згодом сюди так само
підключаються Viber чи WhatsApp, а агенти й платформа не змінюються.
"""

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.masking import mask_pii
from app.db.models import Case, Client, Message, utcnow


@dataclass
class Button:
    text: str
    data: str = ""  # callback-дані; порожньо для спеціальних кнопок
    request_contact: bool = False


@dataclass
class Keyboard:
    rows: list[list[Button]] = field(default_factory=list)
    inline: bool = True
    remove: bool = False  # прибрати попередню reply-клавіатуру


class ChannelSender(Protocol):
    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> None: ...

    async def delete_message(self, chat_id: int, message_id: int) -> None: ...


_senders: dict[str, ChannelSender] = {}


def register_sender(channel: str, sender: ChannelSender) -> None:
    _senders[channel] = sender


def get_sender(channel: str) -> ChannelSender | None:
    return _senders.get(channel)


class DeliveryError(RuntimeError):
    pass


def record_incoming(session: AsyncSession, case: Case, text: str, *, hidden: bool = False) -> Message:
    """Зберегти повідомлення клієнта. hidden=True — текст не зберігаємо (напр., РНОКПП)."""
    shown = "[приховано: персональний код]" if hidden else text
    msg = Message(
        case_id=case.id,
        direction="in",
        author="client",
        text=shown,
        masked_text="" if hidden else mask_pii(text),
    )
    session.add(msg)
    case.last_client_activity = utcnow()
    return msg


def record_note(session: AsyncSession, case: Case, text: str, author: str = "lawyer") -> Message:
    msg = Message(case_id=case.id, direction="note", author=author, text=text)
    session.add(msg)
    return msg


async def send_to_client(
    session: AsyncSession,
    case: Case,
    client: Client,
    text: str,
    *,
    author: str = "assistant",
    keyboard: Keyboard | None = None,
) -> Message:
    """Надіслати клієнту через його канал і зберегти в листуванні."""
    sender = get_sender(case.channel)
    if sender is None:
        raise DeliveryError(f"Канал «{case.channel}» не підключено")
    if case.channel == "telegram":
        if not client.telegram_id:
            raise DeliveryError("У клієнта немає Telegram-контакту")
        await sender.send_text(client.telegram_id, text, keyboard)
    msg = Message(case_id=case.id, direction="out", author=author, text=text)
    session.add(msg)
    return msg
