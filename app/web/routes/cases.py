"""Дашборд, чати, пропозиції відповідей і сторінка звернення."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents import analyst, llm
from app.config import get_settings
from app.core import meetings, research_service
from app.core.settings_store import get_int, get_value, template
from app.core.statuses import DASHBOARD_COLUMNS, CaseStatus, ConflictStatus
from app.core.workflow import TransitionError, audit, is_overdue, set_status
from app.db.models import Case, Document, Draft, Lawyer, Meeting, Message, utcnow
from app.db.session import get_session
from app.messaging import hub
from app.web.auth import current_lawyer, verify_csrf
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(current_lawyer)])
post_router_deps = [Depends(verify_csrf)]


def actor(lawyer: Lawyer) -> str:
    return f"lawyer:{lawyer.username}"


async def load_case(session: AsyncSession, case_id: int) -> Case:
    case = await session.scalar(
        select(Case)
        .where(Case.id == case_id)
        .options(
            selectinload(Case.client),
            selectinload(Case.messages),
            selectinload(Case.documents),
            selectinload(Case.drafts),
            selectinload(Case.parties),
            selectinload(Case.sources),
            selectinload(Case.meetings),
        )
    )
    if case is None:
        raise HTTPException(404, "Звернення не знайдено")
    return case


def back(case_id: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/cases/{case_id}{anchor}", status_code=303)


def sort_key(case: Case):
    return (
        not case.is_urgent,
        not is_overdue(case),
        not case.unread,
        -(case.updated_at or utcnow()).timestamp(),
    )


# ---------------- Дашборд ----------------


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    cases = (
        await session.scalars(
            select(Case)
            .where(Case.status.in_([s.value for s in DASHBOARD_COLUMNS]))
            .options(selectinload(Case.client))
        )
    ).all()
    columns = {s: [] for s in DASHBOARD_COLUMNS}
    for c in sorted(cases, key=sort_key):
        columns[CaseStatus(c.status)].append(c)
    urgent = [c for c in cases if c.is_urgent and c.status not in {CaseStatus.SENT}]

    week_ago = utcnow() - timedelta(days=7)
    month_ago = utcnow() - timedelta(days=30)
    new_week = await session.scalar(
        select(func.count(Case.id)).where(Case.created_at >= week_ago, Case.status != CaseStatus.INACTIVE)
    )
    month_cases = (
        await session.scalars(
            select(Case)
            .where(Case.created_at >= month_ago, Case.status != CaseStatus.INACTIVE)
            .options(selectinload(Case.drafts), selectinload(Case.client))
        )
    ).all()
    with_consent = [c for c in month_cases if c.client.consent_at]
    consulted = [c for c in with_consent if any(d.sent_at for d in c.drafts)]
    hours = [
        (min(d.sent_at for d in c.drafts if d.sent_at) - c.created_at).total_seconds() / 3600
        for c in consulted
    ]
    stats = {
        "new_week": new_week or 0,
        "consult_share": round(100 * len(consulted) / len(with_consent)) if with_consent else None,
        "avg_hours": round(sum(hours) / len(hours), 1) if hours else None,
    }
    # Навантаження: попередження, якщо на один день припадає забагато строків відповіді (ТЗ).
    limit = await get_int(session, "max_due_per_day", 4)
    per_day: dict[str, int] = {}
    for c in cases:
        if c.response_due and c.status in {CaseStatus.IN_REVIEW, CaseStatus.PREPARING, CaseStatus.APPROVED}:
            key = meetings.to_kyiv(c.response_due).strftime("%d.%m")
            per_day[key] = per_day.get(key, 0) + 1
    overload = [(day, n) for day, n in sorted(per_day.items()) if limit and n > limit]
    today_meetings = (
        await session.scalars(
            select(Meeting)
            .where(
                Meeting.kind == "meeting",
                Meeting.status == "scheduled",
                Meeting.starts_at >= utcnow(),
                Meeting.starts_at < utcnow() + timedelta(hours=24),
            )
            .order_by(Meeting.starts_at)
        )
    ).all()
    return render(
        request,
        "dashboard.html",
        columns=columns,
        urgent=urgent,
        stats=stats,
        overload=overload,
        limit=limit,
        today_meetings=today_meetings,
        human_when=meetings.human_when,
        nav="dashboard",
    )


# ---------------- Списки ----------------


@router.get("/chats")
async def chats(
    request: Request, channel: str = "", status: str = "", session: AsyncSession = Depends(get_session)
):
    last_msg = (
        select(Message.case_id, func.max(Message.created_at).label("last_at"))
        .group_by(Message.case_id)
        .subquery()
    )
    query = (
        select(Case, last_msg.c.last_at)
        .join(last_msg, last_msg.c.case_id == Case.id)
        .options(selectinload(Case.client))
        .order_by(Case.unread.desc(), last_msg.c.last_at.desc())
    )
    if channel:
        query = query.where(Case.channel == channel)
    if status:
        query = query.where(Case.status == status)
    rows = (await session.execute(query.limit(300))).all()
    return render(request, "chats.html", rows=rows, channel=channel, status=status, nav="chats")


@router.get("/proposals")
async def proposals(request: Request, session: AsyncSession = Depends(get_session)):
    cases = (
        await session.scalars(
            select(Case)
            .where(Case.status.in_([CaseStatus.IN_REVIEW, CaseStatus.PREPARING, CaseStatus.APPROVED]))
            .options(selectinload(Case.client), selectinload(Case.drafts))
        )
    ).all()
    conflicts = (
        await session.scalars(
            select(Case)
            .where(Case.conflict_status == ConflictStatus.POSSIBLE, Case.status == CaseStatus.CONFLICT_CHECK)
            .options(selectinload(Case.client))
        )
    ).all()
    return render(
        request, "proposals.html", cases=sorted(cases, key=sort_key), conflicts=conflicts, nav="proposals"
    )


# ---------------- Сторінка звернення ----------------


@router.get("/cases/{case_id}")
async def case_page(
    case_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    if case.unread:
        case.unread = False
    await audit(session, actor(lawyer), "view_case", "case", case.id)
    await session.commit()
    draft = next((d for d in case.drafts if d.state == "draft"), None)
    sent_drafts = [d for d in case.drafts if d.state == "sent"]
    return render(
        request,
        "case.html",
        case=case,
        draft=draft,
        sent_drafts=sent_drafts,
        bot_connected=hub.get_sender(case.channel) is not None,
        sources=[s for s in case.sources if not s.excluded],
        excluded_sources=[s for s in case.sources if s.excluded],
        research=case.research or {},
        ai_enabled=llm.enabled(),
        upcoming=[m for m in case.meetings if m.status == "scheduled"],
        offer_slots=[datetime.fromisoformat(x) for x in (case.meeting_offer or {}).get("slots", [])],
        online_enabled=bool((await get_value(session, "online_meeting_url")).strip()),
        human_when=meetings.human_when,
        nav="",
    )


@router.get("/cases/{case_id}/messages")
async def case_messages(case_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Фрагмент листування для автооновлення сторінки."""
    case = await load_case(session, case_id)
    if case.unread:
        case.unread = False
        await session.commit()
    return render(request, "_messages.html", case=case)


@router.post("/cases/{case_id}/reply", dependencies=post_router_deps)
async def reply(
    case_id: int,
    request: Request,
    text: str = Form(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    text = text.strip()
    if not text:
        return back(case_id, "#chat")
    try:
        await hub.send_to_client(session, case, case.client, text, author="lawyer")
    except Exception as exc:
        flash(request, f"Не вдалося надіслати: {exc}", "error")
        return back(case_id, "#chat")
    # Адвокат написав сам — асистент у цьому чаті замовкає (ТЗ).
    case.handoff = "lawyer"
    await audit(session, actor(lawyer), "reply", "case", case.id)
    await session.commit()
    return back(case_id, "#chat")


@router.post("/cases/{case_id}/note", dependencies=post_router_deps)
async def note(
    case_id: int,
    text: str = Form(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    if text.strip():
        hub.record_note(session, case, text.strip())
        await audit(session, actor(lawyer), "note", "case", case.id)
        await session.commit()
    return back(case_id, "#chat")


@router.post("/cases/{case_id}/handoff", dependencies=post_router_deps)
async def handoff(
    case_id: int,
    request: Request,
    to: str = Form(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    if to not in {"lawyer", "assistant"}:
        raise HTTPException(400)
    case.handoff = to
    await audit(session, actor(lawyer), f"handoff_{to}", "case", case.id)
    await session.commit()
    flash(request, "Чат перехоплено: асистент мовчить." if to == "lawyer" else "Чат повернуто асистенту.")
    return back(case_id, "#chat")


@router.post("/cases/{case_id}/draft", dependencies=post_router_deps)
async def save_draft(
    case_id: int,
    request: Request,
    text: str = Form(""),
    action: str = Form("save"),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    form = await request.form()
    draft = next((d for d in case.drafts if d.state == "draft"), None)
    if draft is None:
        draft = Draft(case_id=case.id, author="lawyer", text="")
        session.add(draft)
    if (draft.text or "").strip() != text.strip():
        draft.author = "lawyer"
    draft.text = text.strip()
    if case.status == CaseStatus.IN_REVIEW:
        await set_status(session, case, CaseStatus.PREPARING, actor(lawyer))

    # Галочки «перевірив» біля кожного джерела Дослідника.
    for src in case.sources:
        if not src.excluded:
            src.confirmed = f"confirm_{src.id}" in form

    if action.startswith(("exclude:", "include:")):
        verb, _, raw_id = action.partition(":")
        src = next((x for x in case.sources if str(x.id) == raw_id), None)
        if src is not None:
            src.excluded = verb == "exclude"
            src.confirmed = False
            await audit(session, actor(lawyer), f"source_{verb}", "source", src.id)
        await session.commit()
        return back(case_id, "#sources")

    if action == "approve":
        if not draft.text:
            flash(request, "Порожню відповідь надіслати не можна.", "error")
            await session.commit()
            return back(case_id, "#draft")
        unconfirmed = [x for x in case.sources if not x.excluded and not x.confirmed]
        if unconfirmed:
            await session.commit()
            flash(
                request,
                f"Перевірте й позначте всі джерела ({len(unconfirmed)} не підтверджено) "
                "або приберіть зайві — лише після цього можна надіслати.",
                "error",
            )
            return back(case_id, "#sources")
        footer = await template(session, "consultation_footer")
        try:
            await hub.send_to_client(session, case, case.client, draft.text + footer, author="lawyer")
        except Exception as exc:
            await session.commit()
            flash(request, f"Чернетку збережено, але не надіслано: {exc}", "error")
            return back(case_id, "#draft")
        now = utcnow()
        draft.state = "sent"
        draft.approved_at = now
        draft.sent_at = now
        if case.status != CaseStatus.APPROVED:
            await set_status(session, case, CaseStatus.APPROVED, actor(lawyer), force=True)
        await set_status(session, case, CaseStatus.SENT, actor(lawyer))
        await audit(session, actor(lawyer), "approve_and_send", "draft", draft.id)
        msg = "Консультацію затверджено й надіслано клієнту."
        if ((case.research or {}).get("result") or {}).get("recommend_meeting"):
            msg += " Дослідник радить запропонувати клієнту зустріч (блок «Зустріч» нижче)."
        flash(request, msg)
    else:
        await audit(session, actor(lawyer), "save_draft", "case", case.id)
        flash(request, "Чернетку збережено.")
    await session.commit()
    return back(case_id, "#draft")


@router.post("/cases/{case_id}/research", dependencies=post_router_deps)
async def start_research(
    case_id: int,
    request: Request,
    comment: str = Form(""),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    if not case.card:
        flash(request, "Спершу потрібна картка справи.", "error")
        return back(case_id)
    started = await research_service.start(_sm(), case.id, comment.strip(), actor(lawyer))
    if started:
        flash(request, "Дослідник працює — зазвичай 1–3 хвилини. Сторінка оновиться сама.")
    elif not llm.enabled():
        flash(request, "Дослідник потребує ключа Claude API (ANTHROPIC_API_KEY у .env).", "error")
    else:
        flash(request, "Дослідник уже працює над цією справою.", "error")
    return back(case_id, "#research")


@router.post("/cases/{case_id}/sources/recheck", dependencies=post_router_deps)
async def recheck_sources(case_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    await load_case(session, case_id)
    await research_service.recheck(_sm(), case_id)
    flash(request, "Посилання перевірено ще раз.")
    return back(case_id, "#sources")


@router.post("/cases/{case_id}/meeting/offer", dependencies=post_router_deps)
async def offer_meeting(
    case_id: int,
    request: Request,
    fmt: str = Form("office"),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    try:
        slots = await meetings.offer(session, case, case.client, "online" if fmt == "online" else "office")
    except meetings.MeetingError as exc:
        flash(request, str(exc), "error")
        return back(case_id, "#meeting")
    except Exception as exc:
        flash(request, f"Не вдалося надіслати пропозицію: {exc}", "error")
        return back(case_id, "#meeting")
    await audit(session, actor(lawyer), "meeting_offer", "case", case.id)
    await session.commit()
    flash(request, "Клієнту надіслано варіанти: " + "; ".join(meetings.short_when(x) for x in slots))
    return back(case_id, "#meeting")


@router.post("/meetings/{meeting_id}/cancel", dependencies=post_router_deps)
async def cancel_meeting(
    meeting_id: int,
    request: Request,
    back_to: str = Form("/meetings"),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    meeting = await session.scalar(
        select(Meeting)
        .where(Meeting.id == meeting_id)
        .options(selectinload(Meeting.case).selectinload(Case.client))
    )
    if meeting is None:
        raise HTTPException(404, "Зустріч не знайдено")
    try:
        await meetings.cancel(session, meeting, actor(lawyer))
    except Exception as exc:
        flash(request, f"Скасовано, але клієнта не повідомлено: {exc}", "error")
    await session.commit()
    flash(request, "Зустріч скасовано.")
    target = back_to if back_to.startswith("/") and not back_to.startswith("//") else "/meetings"
    return RedirectResponse(target, status_code=303)


def _sm():
    from app.db.session import sessionmaker

    return sessionmaker()


@router.post("/cases/{case_id}/card", dependencies=post_router_deps)
async def regenerate_card(
    case_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    card = await analyst.build_card(case)
    case.card = card
    if card.get("title"):
        case.title = card["title"]
    case.is_complex = card.get("complexity") == "complex"
    await audit(session, actor(lawyer), "regenerate_card", "case", case.id)
    await session.commit()
    flash(request, "Картку оновлено." if card.get("ai") else "ШІ недоступний — картка зі слів клієнта.")
    return back(case_id)


@router.post("/cases/{case_id}/conflict", dependencies=post_router_deps)
async def resolve_conflict(
    case_id: int,
    request: Request,
    decision: str = Form(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    if decision == "clear":
        case.conflict_status = ConflictStatus.CONFIRMED_CLEAR
        await audit(session, actor(lawyer), "conflict_clear", "case", case.id)
        flow = request.app.state.flow
        if case.intake_step == "conflict_wait" and flow is not None:
            try:
                await flow.start_survey(session, case, case.client)
            except Exception as exc:
                flash(request, f"Рішення збережено, але бот не зміг продовжити опитування: {exc}", "error")
            else:
                flash(request, "Конфлікту немає — асистент продовжив опитування клієнта.")
        else:
            flash(request, "Конфлікту немає.")
    elif decision == "decline":
        case.conflict_status = ConflictStatus.CONFLICT
        try:
            # Ввічлива відмова без пояснення причини, щоб не розкрити дані іншого клієнта (ТЗ).
            await hub.send_to_client(
                session, case, case.client, await template(session, "decline"), author="lawyer"
            )
        except Exception as exc:
            flash(request, f"Відмову не надіслано: {exc}", "error")
        await set_status(session, case, CaseStatus.DECLINED, actor(lawyer))
        case.handoff = "lawyer"
        await audit(session, actor(lawyer), "conflict_decline", "case", case.id)
        flash(request, "Звернення відхилено, клієнту надіслано ввічливу відмову.")
    else:
        raise HTTPException(400)
    await session.commit()
    return back(case_id)


@router.post("/cases/{case_id}/status", dependencies=post_router_deps)
async def change_status(
    case_id: int,
    request: Request,
    status: str = Form(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    try:
        new = CaseStatus(status)
        await set_status(session, case, new, actor(lawyer))
    except (ValueError, TransitionError) as exc:
        flash(request, str(exc), "error")
        return back(case_id)
    await session.commit()
    flash(request, f"Статус: {new.label}")
    return back(case_id)


@router.post("/cases/{case_id}/due", dependencies=post_router_deps)
async def change_due(
    case_id: int,
    request: Request,
    due: str = Form(...),
    notify_client: str = Form(""),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    try:
        # Поле date: вважаємо строком кінець дня за Києвом ≈ 15:00 UTC.
        new_due = datetime.fromisoformat(due) + timedelta(hours=15)
    except ValueError:
        flash(request, "Невірна дата.", "error")
        return back(case_id)
    case.response_due = new_due
    case.due_warned_at = None
    await audit(session, actor(lawyer), "change_due", "case", case.id, due)
    if notify_client:
        text = await template(session, "deadline_moved", date=new_due.strftime("%d.%m.%Y"))
        try:
            await hub.send_to_client(session, case, case.client, text)
        except Exception as exc:
            flash(request, f"Клієнта не повідомлено: {exc}", "error")
    await session.commit()
    flash(request, "Строк відповіді змінено." + (" Клієнта повідомлено." if notify_client else ""))
    return back(case_id)


@router.get("/documents/{doc_id}")
async def document(
    doc_id: int, session: AsyncSession = Depends(get_session), lawyer: Lawyer = Depends(current_lawyer)
):
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "Документ не знайдено")
    path = Path(doc.path).resolve()
    if not path.is_relative_to(get_settings().files_dir.resolve()) or not path.exists():
        raise HTTPException(404, "Файл відсутній")
    await audit(session, actor(lawyer), "open_document", "document", doc.id)
    await session.commit()
    return FileResponse(
        path, media_type=doc.mime or None, filename=doc.filename, content_disposition_type="inline"
    )
