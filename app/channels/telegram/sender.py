"""Відправка повідомлень у Telegram для модуля повідомлень."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.messaging.hub import Keyboard

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000  # офіційний ліміт 4096, лишаємо запас


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Ділить довгий текст по абзацах/рядках, щоб не різати слова."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def to_markup(keyboard: Keyboard | None):
    if keyboard is None:
        return None
    if keyboard.remove:
        return ReplyKeyboardRemove()
    if keyboard.inline:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
                for row in keyboard.rows
            ]
        )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=b.text, request_contact=b.request_contact) for b in row]
            for row in keyboard.rows
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> None:
        parts = split_text(text)
        for i, part in enumerate(parts):
            markup = to_markup(keyboard) if i == len(parts) - 1 else None
            await self.bot.send_message(chat_id, part, reply_markup=markup)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            log.warning("Не вдалося видалити повідомлення %s у чаті %s", message_id, chat_id)
