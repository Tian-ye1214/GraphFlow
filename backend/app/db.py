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
    cur.execute("PRAGMA busy_timeout=30000")  # 大文件摄入期短事务可能与并发写短暂争锁，留足缓冲
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
    if rows and "provider" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE model_configs ADD COLUMN provider VARCHAR NOT NULL DEFAULT 'openai'"
        )
    if rows and "api_version" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE model_configs ADD COLUMN api_version VARCHAR NOT NULL DEFAULT ''"
        )
    if rows and "azure_api_mode" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE model_configs ADD COLUMN azure_api_mode VARCHAR NOT NULL DEFAULT 'legacy'"
        )

    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent_sessions)")).all()
    cols = {row[1] for row in rows}
    if rows and "model_params_json" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE agent_sessions ADD COLUMN model_params_json TEXT NOT NULL DEFAULT '{}'"
        )

    rows = (await conn.exec_driver_sql("PRAGMA table_info(datasets)")).all()
    cols = {row[1] for row in rows}
    dataset_adds = {
        "manifest_json": "TEXT NOT NULL DEFAULT '{}'",
        "status": "VARCHAR NOT NULL DEFAULT 'ready'",
        "imported_rows": "INTEGER NOT NULL DEFAULT 0",
        "original_format": "VARCHAR NOT NULL DEFAULT ''",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "version_of_dataset_id": "INTEGER",
        "header_row": "INTEGER",
        "data_start_row": "INTEGER NOT NULL DEFAULT 1",
        "total_rows_including_header": "INTEGER NOT NULL DEFAULT 0",
        "import_error": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in dataset_adds.items():
        if rows and name not in cols:
            await conn.exec_driver_sql(f"ALTER TABLE datasets ADD COLUMN {name} {ddl}")
    if rows and "run_id" not in cols:
        await conn.exec_driver_sql("ALTER TABLE datasets ADD COLUMN run_id INTEGER")
    if rows and "node_id" not in cols:
        await conn.exec_driver_sql("ALTER TABLE datasets ADD COLUMN node_id VARCHAR")
    if rows:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_datasets_version_parent "
            "ON datasets (version_of_dataset_id, version)"
        )

    rows = (await conn.exec_driver_sql("PRAGMA table_info(run_rows)")).all()
    cols = {row[1] for row in rows}
    run_row_adds = {
        "file_row": "INTEGER",
        "output_ref": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in run_row_adds.items():
        if rows and name not in cols:
            await conn.exec_driver_sql(f"ALTER TABLE run_rows ADD COLUMN {name} {ddl}")
    if rows:
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_run_row_file_row "
            "ON run_rows (run_id, node_id, file_row)"
        )


def get_session_factory() -> async_sessionmaker:
    assert session_factory is not None, "init_db() 未调用"
    return session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """写路径由调用方负责 session.commit()。"""
    async with get_session_factory()() as session:
        yield session
