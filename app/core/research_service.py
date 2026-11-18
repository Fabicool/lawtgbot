"""Запуск Дослідника у фоні, збереження джерел і автоматична перевірка посилань."""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agents import llm, researcher
from app.core import jobs, links, notify
from app.core.workflow import audit
from app.db.models import Case, Draft, Source, utcnow

log = logging.getLogger(__name__)


def is_running(case: Case) -> bool:
    return (case.research or {}).get("state") == "running"


async def start(
    sm: async_sessionmaker[AsyncSession], case_id: int, comment: str = "", actor: str = "system"
) -> bool:
    """Запускає Дослідника у фоні. False — якщо він уже працює або ШІ вимкнено."""
    if not llm.enabled():
        return False
    async with sm() as session:
        case = await session.get(Case, case_id)
        if case is None or is_running(case):
            return False
        case.research = {
            **(case.research or {}),
            "state": "running",
            "started_at": utcnow().isoformat(),
            "error": "",
        }
        await audit(session, actor, "research_start", "case", case_id, comment[:500])
        await session.commit()
    jobs.spawn(run(sm, case_id, comment), name=f"research-{case_id}")
    return True


async def _load(session: AsyncSession, case_id: int) -> Case | None:
    return await session.scalar(
        select(Case).where(Case.id == case_id).options(selectinload(Case.sources), selectinload(Case.drafts))
    )


async def check_sources(sources: list[Source]) -> None:
    async def one(src: Source) -> None:
        result = await links.check_link(src.url, src.case_number if src.kind == "court_decision" else "")
        src.check_status = result.status
        src.check_note = result.note
        src.checked_at = utcnow()

    await asyncio.gather(*(one(s) for s in sources))


async def run(sm: async_sessionmaker[AsyncSession], case_id: int, comment: str = "") -> None:
    async with sm() as session:
        case = await _load(session, case_id)
        if case is None:
            return
        previous = (case.research or {}).get("result")
        card, category = dict(case.card or {}), case.category

    try:
        result = await researcher.research(card, category, previous=previous, comment=comment)
    except Exception:
        log.exception("Дослідник впав на справі %s", case_id)
        result = None

    async with sm() as session:
        case = await _load(session, case_id)
        if case is None:
            return
        research = dict(case.research or {})
        research["finished_at"] = utcnow().isoformat()
        if result is None:
            research.update(
                state="error", error="Дослідник не зміг підготувати відповідь. Спробуйте ще раз пізніше."
            )
            case.research = research
            await session.commit()
            return

        # Підтверджені адвокатом джерела лишаються, решта старих — прибираються.
        kept_urls = set()
        for src in case.sources:
            if src.confirmed and not src.excluded:
                kept_urls.add(src.url)
            else:
                src.excluded = True
        new_sources = []
        for item in result.sources:
            if item.url in kept_urls:
                continue
            src = Source(case_id=case.id, **item.model_dump())
            session.add(src)
            new_sources.append(src)
        await check_sources(new_sources)

        old_text = (previous or {}).get("draft_consultation", "")
        draft = next((d for d in case.drafts if d.state == "draft"), None)
        if draft is None:
            session.add(Draft(case_id=case.id, text=result.draft_consultation, author="researcher"))
        elif not draft.text.strip() or (
            draft.author == "researcher" and draft.text.strip() == old_text.strip()
        ):
            # Адвокат ще не редагував — оновлюємо текст у редакторі.
            draft.text = result.draft_consultation
            draft.author = "researcher"

        comments = list(research.get("comments", []))
        if comment:
            comments.append({"at": utcnow().isoformat(), "text": comment})
        research.update(state="done", error="", result=result.model_dump(), comments=comments)
        case.research = research
        case.unread = True
        await audit(session, "bot", "research_done", "case", case.id, f"джерел: {len(new_sources)}")
        await session.commit()
    await notify.notify_lawyer("research_done", case_id)


async def recheck(sm: async_sessionmaker[AsyncSession], case_id: int) -> None:
    async with sm() as session:
        case = await _load(session, case_id)
        if case is None:
            return
        await check_sources([s for s in case.sources if not s.excluded])
        await session.commit()
