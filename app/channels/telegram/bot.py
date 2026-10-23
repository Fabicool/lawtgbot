"""Telegram-канал: перетворює оновлення aiogram на події Приймальника."""

import io
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.telegram.intake import IncomingEvent, IncomingFile, IntakeFlow
from app.channels.telegram.sender import TelegramSender
from app.messaging import hub

log = logging.getLogger(__name__)


def message_to_event(message: Message) -> IncomingEvent:
    user = message.from_user
    ev = IncomingEvent(
        chat_id=message.chat.id,
        user_id=user.id if user else message.chat.id,
        username=(user.username or "") if user else "",
        message_id=message.message_id,
        text=message.text or message.caption or "",
    )
    if message.contact:
        ev.contact_phone = message.contact.phone_number
        ev.contact_user_id = message.contact.user_id
    if message.document:
        d = message.document
        ev.file = IncomingFile(d.file_id, d.file_name or "document", d.mime_type or "", d.file_size or 0)
    elif message.photo:
        p = message.photo[-1]  # найбільша якість
        ev.file = IncomingFile(p.file_id, f"photo_{message.message_id}.jpg", "image/jpeg", p.file_size or 0)
    return ev


def build_router(flow: IntakeFlow) -> Router:
    router = Router(name="intake")

    @router.message(F.chat.type == "private")
    async def on_message(message: Message) -> None:
        await flow.handle(message_to_event(message))

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await query.answer()
        if query.message is None:
            return
        # Прибираємо кнопки з попереднього повідомлення, щоб їх не натискали повторно.
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        ev = IncomingEvent(
            chat_id=query.message.chat.id,
            user_id=query.from_user.id,
            username=query.from_user.username or "",
            callback=query.data or "",
        )
        await flow.handle(ev)

    return router


def setup(token: str, sm: async_sessionmaker[AsyncSession]) -> tuple[Bot, Dispatcher, IntakeFlow]:
    bot = Bot(token)

    async def fetch_file(file_id: str) -> bytes:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        return buf.getvalue()

    flow = IntakeFlow(sm, fetch_file)
    hub.register_sender("telegram", TelegramSender(bot))
    dp = Dispatcher()
    dp.include_router(build_router(flow))
    return bot, dp, flow
