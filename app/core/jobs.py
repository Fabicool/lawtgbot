"""Фонові завдання в тому ж процесі (Дослідник працює хвилину-дві — не тримаємо HTTP-запит)."""

import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, name: str = "") -> asyncio.Task:
    task = asyncio.create_task(coro, name=name or None)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("Фонове завдання %s завершилось помилкою", name, exc_info=t.exception())

    task.add_done_callback(_done)
    return task


async def wait_all() -> None:
    """Для тестів: дочекатися всіх фонових завдань."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
