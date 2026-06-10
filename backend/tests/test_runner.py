import asyncio
import json

from sqlalchemy import select

from app import crypto
from app.engine import runner
from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)
from app.services import llm

GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}


async def make_run(session_factory, graph=None, rows=3) -> int:
    async with session_factory() as s:
        u = User(username=f"runner{id(graph)}")
        s.add(u)
        await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="qwen", base_url="http://x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc)
        await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=rows)
        s.add(ds)
        await s.flush()
        for i in range(rows):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": f"问{i}"}, ensure_ascii=False)))
        g = json.loads(json.dumps(graph or GRAPH))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] == "llm_synth":
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=json.dumps(g))
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(g))
        s.add(ver)
        await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run)
        await s.commit()
        return run.id


def patch_chat(monkeypatch, fn=None):
    calls: list[str] = []

    async def fake(mc, system, user, params=None, retries=3):
        calls.append(user)
        if fn:
            return fn(user)
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake)
    return calls


async def run_it(session_factory, run_id, cancel=None):
    await runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel or asyncio.Event())


async def get_run(session_factory, run_id) -> Run:
    async with session_factory() as s:
        return await s.get(Run, run_id)


async def test_happy_path(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    assert json.loads(run.stats_json) == {"prompt_tokens": 3, "completion_tokens": 6}
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 3
    assert out_rows[0]["a"] == "答[Q:问0]"
    async with session_factory() as s:
        states = {ns.node_id: ns for ns in (await s.execute(
            select(RunNodeState).where(RunNodeState.run_id == run_id))).scalars()}
    assert states["gen"].total == 3 and states["gen"].done == 3 and states["gen"].failed == 0
    assert states["out"].status == "done"


async def test_row_failure_continues(session_factory, monkeypatch):
    def fn(user):
        if "问1" in user:
            raise RuntimeError("坏行")
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"  # 单行失败不挂任务
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 2
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen", RunRow.status == "failed"))).scalar_one()
    assert "坏行" in rec.error and rec.row_idx == 1


async def test_cancel_before_llm(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    ev = asyncio.Event()
    ev.set()
    await run_it(session_factory, run_id, cancel=ev)
    assert (await get_run(session_factory, run_id)).status == "cancelled"
    assert calls == []


async def test_resume_skips_done_rows(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    async with session_factory() as s:  # 预置 idx0 已完成（模拟上次中断）
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "问0", "a": "旧结果"}], ensure_ascii=False)))
        await s.commit()
    await run_it(session_factory, run_id)
    assert sorted(calls) == ["Q:问1", "Q:问2"]  # 只跑了未完成的两行
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["a"] for r in out_rows} == {"旧结果", "答[Q:问1]", "答[Q:问2]"}


async def test_barrier_failure_fails_run(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"].insert(1, {"id": "proc", "type": "auto_process",
                              "config": {"operations": [{"op": "cast", "column": "q", "to": "int"}]}})
    graph["edges"] = [{"source": "in", "target": "proc", "kind": "normal"},
                      {"source": "proc", "target": "gen", "kind": "normal"},
                      {"source": "gen", "target": "out", "kind": "normal"}]
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed"
    assert "invalid literal" in run.error


async def test_fanout_multiplies_rows(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][1]["config"]["fanout_n"] = 2
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "out")) == 6


async def test_output_save_as_dataset(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["config"] = {"save_as_dataset": True, "dataset_name": "结果集"}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalar_one()
    assert ds.source == "run" and ds.row_count == 3


async def test_barrier_crash_window_repairs_state(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    async with session_factory() as s:  # 模拟崩溃窗口：单元已 done，节点状态卡在 running
        s.add(RunRow(run_id=run_id, node_id="in", row_idx=0, status="done",
                     data_json=json.dumps([{"q": f"问{i}"} for i in range(3)], ensure_ascii=False)))
        s.add(RunNodeState(run_id=run_id, node_id="in", status="running", total=1, done=0))
        await s.commit()
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == "in"))).scalar_one()
    assert ns.status == "done" and ns.done == 1


async def test_cancel_during_llm_node(session_factory, monkeypatch):
    ev = asyncio.Event()
    calls = []

    async def fake(mc, system, user, params=None, retries=3):
        calls.append(user)
        ev.set()  # 第一行处理中触发取消
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][1]["config"]["concurrency"] = 1  # 串行，保证后续行未开始
    run_id = await make_run(session_factory, graph=graph)
    await runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), ev)
    run = await get_run(session_factory, run_id)
    assert run.status == "cancelled"
    assert len(calls) == 1  # 仅第一行调用了 LLM
    async with session_factory() as s:
        recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen"))).scalars().all()
    assert len(recs) == 1 and recs[0].status == "done" and recs[0].row_idx == 0
