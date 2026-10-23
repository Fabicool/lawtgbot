"""Запуск: Telegram-бот і веб-платформа в одному процесі.

python -m app.main
"""

import asyncio
import logging

import uvicorn

from app.config import get_settings
from app.db import session as db
from app.web.app import create_app

log = logging.getLogger("lawtgbot")


def check_config() -> list[str]:
    s = get_settings()
    problems = []
    if not s.fernet_key:
        problems.append("FERNET_KEY не задано (python -m app.cli gen-keys)")
    if s.secret_key in ("", "change-me"):
        problems.append("SECRET_KEY не задано (python -m app.cli gen-keys)")
    return problems


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    problems = check_config()
    if problems:
        for p in problems:
            log.error(p)
        raise SystemExit(1)

    s = get_settings()
    sm = db.init_engine()
    await db.create_all()

    bot = dp = flow = None
    if s.telegram_bot_token:
        from app.channels.telegram.bot import setup

        bot, dp, flow = setup(s.telegram_bot_token, sm)
        me = await bot.get_me()
        log.info("Telegram-бот @%s підключено", me.username)
    else:
        log.warning("TELEGRAM_BOT_TOKEN не задано — працює лише веб-платформа")
    if not s.ai_enabled:
        log.warning("ANTHROPIC_API_KEY не задано — бот працює за анкетою без ШІ")

    app = create_app(flow)
    server = uvicorn.Server(uvicorn.Config(app, host=s.web_host, port=s.web_port, log_level="info"))
    log.info("Платформа: %s", s.base_url)

    tasks = [asyncio.create_task(server.serve())]
    if dp is not None:
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()
    finally:
        if dp is not None:
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass
            await bot.session.close()
        server.should_exit = True
        await db.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
