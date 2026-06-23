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
