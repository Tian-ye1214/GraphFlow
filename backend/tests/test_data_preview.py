import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.data_preview import WorkflowDataPreview
from app.models import Base, Dataset, DatasetRow, Run, RunRow, User, Workflow, WorkflowVersion


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(session_factory):
    async with session_factory() as s:
        user = User(username="u")
        other = User(username="other")
        s.add_all([user, other])
        await s.flush()

        ds = Dataset(
            user_id=user.id,
            name="questions",
            row_count=2,
            columns_json=json.dumps(["q", "answer"]),
        )
        other_ds = Dataset(
            user_id=other.id,
            name="private",
            row_count=1,
            columns_json=json.dumps(["secret"]),
        )
        s.add_all([ds, other_ds])
        await s.flush()
        s.add_all([
            DatasetRow(dataset_id=ds.id, idx=0, data_json=json.dumps({"q": "one", "answer": "A" * 120})),
            DatasetRow(dataset_id=ds.id, idx=1, data_json=json.dumps({"q": "two", "answer": "B"})),
            DatasetRow(dataset_id=other_ds.id, idx=0, data_json=json.dumps({"secret": "nope"})),
        ])

        wf = Workflow(
            user_id=user.id,
            name="wf",
            graph_json=json.dumps({
                "nodes": [
                    {"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
                    {"id": "llm", "type": "llm_synth", "config": {"output_column": "model_answer"}},
                ],
                "edges": [{"source": "in", "target": "llm"}],
            }),
        )
        other_wf = Workflow(
            user_id=other.id,
            name="private-wf",
            graph_json=json.dumps({
                "nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [other_ds.id]}}],
                "edges": [],
            }),
        )
        s.add_all([wf, other_wf])
        await s.flush()

        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=wf.graph_json)
        s.add(ver)
        await s.flush()
        run = Run(user_id=user.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.flush()
        s.add(RunRow(
            run_id=run.id,
            node_id="in",
            row_idx=0,
            status="done",
            data_json=json.dumps([{"q": "run-one", "answer": "run-A"}]),
        ))
        await s.commit()
        return user.id, other_wf.id, wf.id, run.id


@pytest.mark.asyncio
async def test_preview_auto_prefers_latest_run(session_factory):
    user_id, _other_wf_id, wf_id, run_id = await _seed(session_factory)

    out = await WorkflowDataPreview(session_factory, user_id, cell_char_limit=20).preview_workflow_data(
        workflow_id=wf_id,
        node_id="llm",
        source="auto",
        limit=5,
    )
    data = json.loads(out)

    assert data["source"] == "latest_run"
    assert data["run_id"] == run_id
    assert data["columns"] == ["q", "answer"]
    assert data["rows"] == [{"q": "run-one", "answer": "run-A"}]


@pytest.mark.asyncio
async def test_preview_dataset_fallback_truncates_values(session_factory):
    user_id, _other_wf_id, wf_id, _run_id = await _seed(session_factory)

    out = await WorkflowDataPreview(session_factory, user_id, cell_char_limit=20).preview_workflow_data(
        workflow_id=wf_id,
        source="dataset",
        limit=5,
    )
    data = json.loads(out)

    assert data["source"] == "dataset"
    assert data["columns"] == ["q", "answer"]
    assert data["rows"][0]["answer"].endswith("[truncated]")
    assert data["truncated"] is True


@pytest.mark.asyncio
async def test_preview_refuses_other_users_workflow(session_factory):
    user_id, other_wf_id, _wf_id, _run_id = await _seed(session_factory)

    out = await WorkflowDataPreview(session_factory, user_id).preview_workflow_data(
        workflow_id=other_wf_id,
        source="dataset",
    )
    data = json.loads(out)

    assert data["source"] == "none"
    assert data["rows"] == []
    assert data["error"] == "workflow_not_found"
