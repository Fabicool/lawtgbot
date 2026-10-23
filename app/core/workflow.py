"""Керуючий модуль (не ШІ): статуси, строки, журнал дій."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_store import get_int
from app.core.statuses import CaseStatus, can_transition
from app.core.workdays import add_workdays
from app.db.models import AuditLog, Case, utcnow


class TransitionError(ValueError):
    pass


async def audit(
    session: AsyncSession,
    actor: str,
    action: str,
    object_type: str = "",
    object_id: int | None = None,
    details: str = "",
) -> None:
    session.add(
        AuditLog(actor=actor, action=action, object_type=object_type, object_id=object_id, details=details)
    )


async def set_status(
    session: AsyncSession, case: Case, new: CaseStatus, actor: str, *, force: bool = False
) -> None:
    old = CaseStatus(case.status)
    if old == new:
        return
    if not force and not can_transition(old, new):
        raise TransitionError(f"Неможливий перехід {old.label} → {new.label}")
    case.status = new
    if new == CaseStatus.IN_REVIEW and case.response_due is None:
        key = "response_days_complex" if case.is_complex else "response_days"
        days = await get_int(session, key, 7 if case.is_complex else 2)
        case.response_due = add_workdays(utcnow(), days)
    await audit(session, actor, "status", "case", case.id, f"{old.value} → {new.value}")


def is_overdue(case: Case) -> bool:
    return (
        case.response_due is not None
        and case.status in {CaseStatus.IN_REVIEW, CaseStatus.PREPARING, CaseStatus.APPROVED}
        and case.response_due < utcnow()
    )
