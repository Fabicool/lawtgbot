"""Службові команди.

python -m app.cli gen-keys                 — згенерувати SECRET_KEY і FERNET_KEY для .env
python -m app.cli create-lawyer <логін>    — створити вхід адвоката (пароль + 2FA)
python -m app.cli reset-2fa <логін>        — новий QR-код 2FA (якщо загубили телефон)
python -m app.cli import-excel <файл.xlsx> — імпорт клієнтів з Excel-шаблону
"""

import asyncio
import getpass
import secrets
import sys
from pathlib import Path

import segno
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.excel import import_xlsx
from app.db import session as db
from app.db.models import Lawyer
from app.web.auth import hash_password, new_totp_secret, totp_uri

QR_FILE = Path("lawyer-2fa.png")


def gen_keys() -> None:
    print("Додайте ці рядки у файл .env (і нікому не показуйте):\n")
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"FERNET_KEY={Fernet.generate_key().decode()}")


def _show_qr(secret: str, username: str) -> None:
    uri = totp_uri(secret, username)
    qr = segno.make(uri)
    qr.save(QR_FILE, scale=8)
    print("\nВідскануйте QR-код застосунком Google Authenticator / Microsoft Authenticator / 1Password:\n")
    try:
        qr.terminal(compact=True)
    except Exception:
        pass
    print(f"\nQR також збережено у файл {QR_FILE.resolve()} — ВИДАЛІТЬ його після сканування.")
    print(f"Або введіть ключ вручну: {secret}")


async def _init() -> None:
    db.init_engine()
    await db.create_all()


async def create_lawyer(username: str) -> None:
    await _init()
    async with db.sessionmaker()() as s:
        if await s.scalar(select(Lawyer).where(Lawyer.username == username)):
            print(f"Користувач «{username}» уже існує.")
            return
        while True:
            pwd = getpass.getpass("Пароль (мінімум 10 символів): ")
            if len(pwd) < 10:
                print("Закороткий пароль.")
                continue
            if pwd != getpass.getpass("Повторіть пароль: "):
                print("Паролі не збігаються.")
                continue
            break
        secret = new_totp_secret()
        s.add(Lawyer(username=username, password_hash=hash_password(pwd), totp_secret=secret))
        await s.commit()
    print(f"\nКористувача «{username}» створено.")
    _show_qr(secret, username)
    await db.dispose()


async def reset_2fa(username: str) -> None:
    await _init()
    async with db.sessionmaker()() as s:
        lawyer = await s.scalar(select(Lawyer).where(Lawyer.username == username))
        if lawyer is None:
            print("Користувача не знайдено.")
            return
        lawyer.totp_secret = new_totp_secret()
        await s.commit()
        _show_qr(lawyer.totp_secret, username)
    await db.dispose()


async def import_excel(path: str) -> None:
    await _init()
    async with db.sessionmaker()() as s:
        result = await import_xlsx(s, Path(path).read_bytes())
        await s.commit()
    print(
        f"Клієнтів: {result.clients}, інших сторін: {result.parties}, пропущено дублікатів: {result.skipped}"
    )
    for err in result.errors:
        print("  !", err)
    await db.dispose()


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, *args = argv
    if cmd == "gen-keys":
        gen_keys()
    elif cmd == "create-lawyer" and args:
        asyncio.run(create_lawyer(args[0]))
    elif cmd == "reset-2fa" and args:
        asyncio.run(reset_2fa(args[0]))
    elif cmd == "import-excel" and args:
        asyncio.run(import_excel(args[0]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
