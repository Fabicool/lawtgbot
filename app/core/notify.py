"""Сповіщення адвоката email-листом.

У листі немає подробиць справи — лише номер і посилання на платформу (адвокатська таємниця).
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings

log = logging.getLogger(__name__)


async def send_email(subject: str, body: str) -> bool:
    s = get_settings()
    if not (s.smtp_host and s.lawyer_email):
        log.info("Email не налаштовано, сповіщення лише на дашборді: %s", subject)
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = s.lawyer_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=s.smtp_password or None,
            start_tls=s.smtp_port == 587,
            use_tls=s.smtp_port == 465,
            timeout=20,
        )
        return True
    except Exception:  # сповіщення не має ламати роботу бота
        log.exception("Не вдалося надіслати email")
        return False


EVENT_SUBJECTS = {
    "urgent": "ТЕРМІНОВО: звернення #{id}",
    "in_review": "Нове звернення #{id} чекає на розгляд",
    "handoff": "Клієнт просить адвоката: звернення #{id}",
    "client_message": "Нове повідомлення у зверненні #{id}",
}


async def notify_lawyer(event: str, case_id: int, extra: str = "") -> bool:
    subject = EVENT_SUBJECTS.get(event, "Звернення #{id}").format(id=case_id)
    url = f"{get_settings().base_url.rstrip('/')}/cases/{case_id}"
    body = f"{subject}\n\nВідкрити на платформі: {url}\n"
    if extra:
        body += f"\n{extra}\n"
    return await send_email(subject, body)
