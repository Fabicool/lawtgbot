"""Налаштування: тексти бота, анкети, переліки документів, строки — без програміста."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import defaults
from app.core.settings_store import default_value, get_value, set_value
from app.core.workflow import audit
from app.db.models import AuditLog, Lawyer, Setting
from app.db.session import get_session
from app.web.auth import current_lawyer, verify_csrf
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(current_lawyer)])

GENERAL_FIELDS = [
    ("lawyer_name", "ПІБ адвоката (як підписувати повідомлення)", "input"),
    ("lawyer_phone", "Телефон для термінових звернень", "input"),
    ("office_address", "Адреса офісу", "input"),
    ("working_hours", "Робочі години", "input"),
    ("response_days", "Строк відповіді, робочих днів", "number"),
    ("response_days_complex", "Строк для складних справ, робочих днів", "number"),
    ("daily_limit", "Ліміт нових звернень на день (0 — без ліміту)", "number"),
    ("max_clarify_rounds", "Раундів уточнювальних питань ШІ (макс.)", "number"),
]

TEMPLATE_LABELS = {
    "greeting": "Перше повідомлення",
    "no_consent": "Якщо клієнт не дав згоди",
    "urgent_question": "Питання про терміновість",
    "urgent_reply": "Відповідь на термінове звернення",
    "ask_name": "Запит ПІБ",
    "ask_phone": "Запит телефону",
    "ask_opponent": "Питання про другу сторону",
    "ask_tax_id": "Запит РНОКПП (за збігу ПІБ)",
    "survey_intro": "Перед опитуванням",
    "docs_intro": "Запит документів + інструкція сканування",
    "received_ack": "Підтвердження прийому",
    "received_ack_complex": "Підтвердження прийому (складна справа)",
    "conflict_pending": "Можливий конфлікт: «заявка на розгляді»",
    "handoff": "Клієнт просить адвоката",
    "lawyer_will_answer": "Повідомлення передано адвокату",
    "daily_limit": "Ліміт звернень на день",
    "decline": "Ввічлива відмова",
    "consultation_footer": "Підпис під консультацією",
}


def all_keys() -> list[str]:
    keys = [k for k, _, _ in GENERAL_FIELDS]
    keys += [f"tpl.{k}" for k in TEMPLATE_LABELS]
    keys.append("base_questions")
    keys += [f"questions.{c}" for c in defaults.CATEGORIES]
    keys += [f"documents.{c}" for c in defaults.CATEGORIES]
    return keys


@router.get("/settings")
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    values = {k: await get_value(session, k) for k in all_keys()}
    log = (await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(50))).all()
    return render(
        request,
        "settings.html",
        values=values,
        general=GENERAL_FIELDS,
        template_labels=TEMPLATE_LABELS,
        log=log,
        ai_enabled=get_settings().ai_enabled,
        email_enabled=bool(get_settings().smtp_host),
        model=get_settings().claude_model,
        nav="settings",
    )


@router.post("/settings", dependencies=[Depends(verify_csrf)])
async def save_settings(
    request: Request, session: AsyncSession = Depends(get_session), lawyer: Lawyer = Depends(current_lawyer)
):
    form = await request.form()
    changed = 0
    for key in all_keys():
        if key not in form:
            continue
        value = str(form[key]).replace("\r\n", "\n").strip()
        if value == (await get_value(session, key)).strip():
            continue
        if value == default_value(key).strip():
            row = await session.get(Setting, key)
            if row is not None:
                await session.delete(row)
        else:
            await set_value(session, key, value)
        changed += 1
    await audit(session, f"lawyer:{lawyer.username}", "settings", details=f"змінено полів: {changed}")
    await session.commit()
    flash(request, f"Збережено (змінено полів: {changed}).")
    return RedirectResponse("/settings", status_code=303)
