"""Вхід адвоката: пароль + одноразовий код 2FA (Google Authenticator тощо)."""

import secrets
import time
from collections import defaultdict

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lawyer
from app.db.session import get_session

_hasher = PasswordHasher()

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60
_attempts: dict[str, list[float]] = defaultdict(list)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Платформа адвоката")


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def too_many_attempts(key: str) -> bool:
    now = time.time()
    _attempts[key] = [t for t in _attempts[key] if now - t < WINDOW_SECONDS]
    return len(_attempts[key]) >= MAX_ATTEMPTS


def register_failure(key: str) -> None:
    _attempts[key].append(time.time())


def reset_attempts(key: str) -> None:
    _attempts.pop(key, None)


async def authenticate(session: AsyncSession, username: str, password: str, code: str) -> Lawyer | None:
    lawyer = await session.scalar(select(Lawyer).where(Lawyer.username == username.strip()))
    if lawyer is None:
        verify_password(_hasher.hash("timing"), password)  # однаковий час відповіді
        return None
    if not verify_password(lawyer.password_hash, password):
        return None
    if not verify_totp(lawyer.totp_secret, code):
        return None
    return lawyer


class LoginRequired(Exception):
    pass


async def current_lawyer(request: Request, session: AsyncSession = Depends(get_session)) -> Lawyer:
    lawyer_id = request.session.get("lawyer_id")
    if not lawyer_id:
        raise LoginRequired()
    lawyer = await session.get(Lawyer, lawyer_id)
    if lawyer is None:
        request.session.clear()
        raise LoginRequired()
    return lawyer


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method != "POST":
        return
    form = await request.form()
    sent = form.get("csrf") or request.headers.get("X-CSRF-Token", "")
    if not sent or not secrets.compare_digest(str(sent), request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="Сесія застаріла. Оновіть сторінку й спробуйте ще раз.")
