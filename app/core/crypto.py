"""Шифрування персональних кодів (РНОКПП/ЄДРПОУ) у базі."""

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY не задано в .env (згенеруйте: python -m app.cli gen-keys)")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def code_hash(code: str) -> str:
    """Детермінований хеш коду для пошуку збігів без розшифрування."""
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return ""
    key = get_settings().fernet_key.encode()
    return hmac.new(key, digits.encode(), hashlib.sha256).hexdigest()
