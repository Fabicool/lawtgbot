import pyotp
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.agents import llm
from app.config import get_settings
from app.db import session as db_session
from app.messaging import hub


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_text(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    @property
    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.chdir(tmp_path)  # щоб не підхопився .env з репозиторію
    get_settings.cache_clear()
    llm.set_client(None)
    yield
    get_settings.cache_clear()
    llm.set_client(None)


@pytest.fixture
async def sm(tmp_path):
    maker = db_session.init_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db_session.create_all()
    yield maker
    await db_session.dispose()


@pytest.fixture
def sender():
    s = FakeSender()
    hub.register_sender("telegram", s)
    return s


SECRET = pyotp.random_base32()


@pytest.fixture
async def web(sm, sender):
    """Веб-платформа з одним адвокатом (логін adv / password123, 2FA — SECRET)."""
    from app.channels.telegram.intake import IntakeFlow
    from app.db.models import Lawyer
    from app.web.app import create_app
    from app.web.auth import hash_password

    async with sm() as s:
        s.add(Lawyer(username="adv", password_hash=hash_password("password123"), totp_secret=SECRET))
        await s.commit()

    async def fetch(_):
        return b""

    flow = IntakeFlow(sm, fetch)
    app = create_app(flow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.flow = flow
        yield client
