"""Підключення до бази. Один engine на процес."""

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    settings = get_settings()
    url = url or settings.db_url
    if url.startswith("sqlite") and ":memory:" not in url:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


def _sql_default(column) -> str:
    default = column.default.arg if column.default is not None else None
    if callable(default):
        try:
            default = default(None)
        except TypeError:
            default = default()
    if default is None:
        return "NULL"
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, int | float):
        return str(default)
    if isinstance(default, dict):
        return "'{}'"
    if isinstance(default, list):
        return "'[]'"
    return "'" + str(default).replace("'", "''") + "'"


def _add_missing_columns(conn: Connection) -> None:
    """Проста міграція: нові колонки додаються в уже наявну базу (дані не губляться)."""
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            conn.execute(
                text(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type} "
                    f"DEFAULT {_sql_default(column)}"
                )
            )


async def create_all() -> None:
    assert _engine is not None, "init_engine() не викликано"
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session
