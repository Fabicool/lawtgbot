"""Дашборд, чати, пропозиції відповідей і сторінка звернення."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents import analyst
from app.config import get_settings
from app.core.settings_store import template
from app.core.statuses import DASHBOARD_COLUMNS, CaseStatus, ConflictStatus
from app.core.workflow import TransitionError, audit, is_overdue, set_status
from app.db.models import Case, Document, Draft, Lawyer, Message, utcnow
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
    return render(request, "dashboard.html", columns=columns, urgent=urgent, stats=stats, nav="dashboard")


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
    draft = next((d for d in case.drafts if d.state == "draft"), None)
    if draft is None:
        draft = Draft(case_id=case.id)
        session.add(draft)
    draft.text = text.strip()
    if case.status == CaseStatus.IN_REVIEW:
        await set_status(session, case, CaseStatus.PREPARING, actor(lawyer))

    if action == "approve":
        if not draft.text:
            flash(request, "Порожню відповідь надіслати не можна.", "error")
            await session.commit()
            return back(case_id, "#draft")
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
        # Нові питання клієнта після консультації — до адвоката, асистент не консультує сам (ТЗ).
        case.handoff = "lawyer"
        await audit(session, actor(lawyer), "approve_and_send", "draft", draft.id)
        flash(request, "Консультацію затверджено й надіслано клієнту.")
    else:
        await audit(session, actor(lawyer), "save_draft", "case", case.id)
        flash(request, "Чернетку збережено.")
    await session.commit()
    return back(case_id, "#draft")


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
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    case = await load_case(session, case_id)
    try:
        # Поле date: вважаємо строком кінець дня за Києвом ≈ 15:00 UTC.
        case.response_due = datetime.fromisoformat(due) + timedelta(hours=15)
    except ValueError:
        flash(request, "Невірна дата.", "error")
        return back(case_id)
    await audit(session, actor(lawyer), "change_due", "case", case.id, due)
    await session.commit()
    flash(request, "Строк відповіді змінено.")
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
