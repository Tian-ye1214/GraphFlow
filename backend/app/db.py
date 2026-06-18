from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

engine: AsyncEngine | None = None
session_factory: async_sessionmaker | None = None


def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


async def init_db() -> None:
    global engine, session_factory
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.db_url)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "sqlite":
            await _migrate_sqlite_schema(conn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def _migrate_sqlite_schema(conn) -> None:
    rows = (await conn.exec_driver_sql("PRAGMA table_info(model_configs)")).all()
    cols = {row[1] for row in rows}
    if "provider" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE model_configs ADD COLUMN provider VARCHAR NOT NULL DEFAULT 'openai'"
        )
    if "api_version" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE model_configs ADD COLUMN api_version VARCHAR NOT NULL DEFAULT ''"
        )


def get_session_factory() -> async_sessionmaker:
    assert session_factory is not None, "init_db() 未调用"
    return session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """写路径由调用方负责 session.commit()。"""
    async with get_session_factory()() as session:
        yield session
