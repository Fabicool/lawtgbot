"""Налаштування з .env. Секрети ніколи не зберігаються в коді чи репозиторії."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = ""

    # Claude API (необов'язково: без ключа бот працює за сценарієм)
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"

    # Безпека
    secret_key: str = "change-me"  # підпис cookie сесії
    fernet_key: str = ""  # шифрування РНОКПП/ЄДРПОУ в базі

    # Сховище
    data_dir: Path = Path("data")
    database_url: str = ""

    # Веб-платформа
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    base_url: str = "http://localhost:8000"
    cookie_secure: bool = False  # True на VPS з HTTPS

    # Email-сповіщення адвокату
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    lawyer_email: str = ""

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'app.db'}"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
