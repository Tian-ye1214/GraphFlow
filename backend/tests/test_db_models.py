import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Run, RunNodeState, RunRow, User, Workflow, WorkflowVersion


async def test_create_user_and_query(session_factory):
    async with session_factory() as s:
        s.add(User(username="alice", display_name="Alice"))
        await s.commit()
    async with session_factory() as s:
        u = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
        assert u.max_llm_concurrency == 8
        assert u.auth_provider == "dev"


async def test_run_row_unique_unit(session_factory):
    async with session_factory() as s:
        u = User(username="bob")
        s.add(u)
        await s.flush()
        wf = Workflow(user_id=u.id, name="wf", graph_json="{}")
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}")
        s.add(ver)
        await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run)
        await s.flush()
        s.add(RunRow(run_id=run.id, node_id="n1", row_idx=0, status="done", data_json="[]"))
        s.add(RunNodeState(run_id=run.id, node_id="n1", status="done", total=1, done=1))
        await s.commit()
        assert run.status == "queued"
    async with session_factory() as s2:
        s2.add(RunRow(run_id=run.id, node_id="n1", row_idx=0))
        with pytest.raises(IntegrityError):
            await s2.commit()


async def test_migrate_drops_legacy_is_template_column(tmp_path):
    """已废弃 is_template 列从既有库幂等剔除（回归：曾删模型字段却没配 DROP COLUMN →
    既有库残留 NOT NULL 列把建/导/复制工作流全挂）。"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.db import _migrate_sqlite_schema
    eng = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    try:
        async with eng.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE workflows (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR, "
                "graph_json TEXT, is_template BOOLEAN NOT NULL DEFAULT 0, "
                "created_at TEXT, updated_at TEXT)")
            assert "is_template" in {r[1] for r in (
                await conn.exec_driver_sql("PRAGMA table_info(workflows)")).all()}
            await _migrate_sqlite_schema(conn)
            cols = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(workflows)")).all()}
            assert "is_template" not in cols
            await _migrate_sqlite_schema(conn)  # 幂等：列已不存在则跳过，再跑不报错
    finally:
        await eng.dispose()
