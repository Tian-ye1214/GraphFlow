import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.engine.graph import Graph, Node
from app.engine.runner import _run_qc_node
from app.models import Base, ModelConfig, RunRow, User


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


@pytest.mark.asyncio
async def test_qc_feedback_column_is_available_to_rescan_and_final_rows(session_factory, monkeypatch):
    async with session_factory() as s:
        user = User(username="u")
        s.add(user)
        await s.flush()
        judge_model = ModelConfig(user_id=user.id, name="judge", model_name="m", base_url="x", api_key_enc="")
        regen_model = ModelConfig(user_id=user.id, name="regen", model_name="m", base_url="x", api_key_enc="")
        s.add_all([judge_model, regen_model])
        await s.commit()
        user_id = user.id
        judge_id = judge_model.id
        regen_id = regen_model.id

    async def fake_judge(_cfg, row, _jmcs, _pass_k, _user_sem):
        if row.get("answer") == "fixed":
            return True, "通过", {"prompt_tokens": 1, "completion_tokens": 1}, []
        return False, "too short", {"prompt_tokens": 1, "completion_tokens": 1}, []

    async def fake_regen(_cfg, row, _tmc, _user_sem):
        assert row["qc_feedback"] == "too short"
        return [{**row, "answer": "fixed"}], {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr("app.engine.nodes.run_qc_judge_row", fake_judge)
    monkeypatch.setattr("app.engine.nodes.run_llm_synth_row", fake_regen)

    qc = Node(
        id="qc",
        type="qc",
        config={"model_config_id": judge_id, "max_rounds": 1, "feedback_column": "qc_feedback"},
    )
    graph = Graph(
        nodes=[
            qc,
            Node(id="regen", type="llm_synth", config={"model_config_id": regen_id}),
        ],
        edges=[{"source": "qc", "target": "regen", "kind": "rescan"}],
    )

    await _run_qc_node(
        session_factory,
        run_id=1,
        user_id=user_id,
        graph=graph,
        node=qc,
        inputs=[{"q": "q1", "answer": "bad"}],
        user_sem=asyncio.Semaphore(1),
        cancel_event=asyncio.Event(),
    )

    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(RunRow.node_id == "qc"))).scalar_one()

    rows = json.loads(rec.data_json)
    assert rows == [{"q": "q1", "answer": "fixed", "qc_feedback": "too short"}]
