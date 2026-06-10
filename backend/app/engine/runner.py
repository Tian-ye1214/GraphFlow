import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine import nodes
from app.engine.graph import Graph, Node, parse_graph, topo_order, upstream_ids, validate_graph
from app.models import DatasetRow, ModelConfig, Run, RunNodeState, RunRow, WorkflowVersion
from app.routers.datasets import create_dataset


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def execute_run(run_id: int, session_factory: async_sessionmaker,
                      user_sem: asyncio.Semaphore, cancel_event: asyncio.Event) -> None:
    """运行入口：任何未捕获异常都落为 run.failed，不向上抛。"""
    try:
        await _execute(run_id, session_factory, user_sem, cancel_event)
    except Exception as e:
        async with session_factory() as s:
            run = await s.get(Run, run_id)
            run.status = "failed"
            run.error = str(e)
            run.finished_at = _now()
            await s.commit()


async def _execute(run_id, session_factory, user_sem, cancel_event):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        run.status = "running"
        run.started_at = run.started_at or _now()
        await s.commit()
        user_id = run.user_id
        stats = json.loads(run.stats_json)
    stats.setdefault("prompt_tokens", 0)
    stats.setdefault("completion_tokens", 0)
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled", stats)
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event, stats)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled", stats)
    await _finish(session_factory, run_id, "completed", stats)


async def _finish(session_factory, run_id, status, stats):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        run.status = status
        run.stats_json = json.dumps(stats)
        run.finished_at = _now()
        await s.commit()


async def _node_outputs(session_factory, run_id, node_id) -> list[dict]:
    async with session_factory() as s:
        recs = (await s.execute(
            select(RunRow).where(RunRow.run_id == run_id, RunRow.node_id == node_id,
                                 RunRow.status == "done").order_by(RunRow.row_idx)
        )).scalars().all()
    out: list[dict] = []
    for r in recs:
        out.extend(json.loads(r.data_json))
    return out


async def _node_inputs(session_factory, run_id, graph: Graph, node: Node) -> list[dict]:
    rows: list[dict] = []
    for uid in upstream_ids(graph, node.id):
        rows.extend(await _node_outputs(session_factory, run_id, uid))
    return rows


async def _write_unit(session_factory, run_id, node_id, row_idx, status, out_rows, error):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.row_idx == row_idx
        ))).scalar_one_or_none()
        if rec is None:
            rec = RunRow(run_id=run_id, node_id=node_id, row_idx=row_idx, attempt=0)
            s.add(rec)
        rec.status = status
        rec.data_json = json.dumps(out_rows, ensure_ascii=False)
        rec.error = error
        rec.attempt = (rec.attempt or 0) + 1
        await s.commit()


async def _set_node_state(session_factory, run_id, node_id, *, status, total, done, failed):
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == node_id
        ))).scalar_one_or_none()
        if ns is None:
            ns = RunNodeState(run_id=run_id, node_id=node_id)
            s.add(ns)
        ns.status, ns.total, ns.done, ns.failed = status, total, done, failed
        await s.commit()


async def _run_barrier_node(session_factory, run_id, user_id, node: Node, inputs):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
    if rec is not None and rec.status == "done":
        return  # 断点续跑：已完成
    await _set_node_state(session_factory, run_id, node.id, status="running", total=1, done=0, failed=0)
    try:
        out = await _barrier_output(session_factory, user_id, node, inputs)
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, status="failed", total=1, done=0, failed=1)
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", out, "")
    await _set_node_state(session_factory, run_id, node.id, status="done", total=1, done=1, failed=0)


async def _barrier_output(session_factory, user_id, node: Node, inputs) -> list[dict]:
    cfg = node.config
    if node.type == "input":
        rows: list[dict] = []
        async with session_factory() as s:
            for ds_id in cfg.get("dataset_ids", []):
                recs = (await s.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds_id)
                                        .order_by(DatasetRow.idx))).scalars().all()
                rows.extend(json.loads(r.data_json) for r in recs)
        return rows
    if node.type == "auto_process":
        return nodes.apply_operations(inputs, cfg.get("operations", []), seed=cfg.get("seed"))
    if node.type == "output":
        if cfg.get("save_as_dataset"):
            async with session_factory() as s:
                await create_dataset(s, user_id, cfg.get("dataset_name", "运行结果"),
                                     inputs, source="run")
        return inputs
    raise ValueError(f"未知节点类型: {node.type}")


async def _run_llm_node(session_factory, run_id, user_id, node: Node, inputs,
                        user_sem, cancel_event, stats):
    cfg = node.config
    async with session_factory() as s:
        mc = await s.get(ModelConfig, cfg.get("model_config_id"))
        if mc is None or mc.user_id != user_id:
            raise ValueError(f"节点 {node.id}: 模型配置不存在")
        existing = (await s.execute(select(RunRow.row_idx, RunRow.status).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id))).all()
    done_idx = {idx for idx, st in existing if st == "done"}
    failed_idx = {idx for idx, st in existing if st == "failed"}
    total = len(inputs)
    done_count, failed_count = len(done_idx), len(failed_idx)
    await _set_node_state(session_factory, run_id, node.id, status="running",
                          total=total, done=done_count, failed=failed_count)
    todo = [i for i in range(total) if i not in done_idx and i not in failed_idx]
    node_sem = asyncio.Semaphore(cfg.get("concurrency", 4))
    lock = asyncio.Lock()

    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem)
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "")
                async with lock:
                    stats["prompt_tokens"] += usage["prompt_tokens"]
                    stats["completion_tokens"] += usage["completion_tokens"]
                    done_count += 1
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                async with lock:
                    failed_count += 1
            await _set_node_state(session_factory, run_id, node.id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
    if not cancel_event.is_set():
        await _set_node_state(session_factory, run_id, node.id, status="done",
                              total=total, done=done_count, failed=failed_count)
