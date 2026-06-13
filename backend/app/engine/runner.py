import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
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
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event)
        elif node.type == "qc":
            await _run_qc_node(session_factory, run_id, user_id, graph, node, inputs,
                               user_sem, cancel_event)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
    await _finish(session_factory, run_id, "completed")


async def _finish(session_factory, run_id, status):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        sums = (await s.execute(
            select(func.coalesce(func.sum(RunRow.prompt_tokens), 0),
                   func.coalesce(func.sum(RunRow.completion_tokens), 0))
            .where(RunRow.run_id == run_id, RunRow.status == "done"))).one()
        run.stats_json = json.dumps({"prompt_tokens": sums[0], "completion_tokens": sums[1]})
        run.status = status
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


async def _write_unit(session_factory, run_id, node_id, row_idx, status, out_rows, error,
                      usage: dict | None = None, qc_round: int = 0):
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
        rec.qc_round = qc_round
        if usage:
            rec.prompt_tokens = usage["prompt_tokens"]
            rec.completion_tokens = usage["completion_tokens"]
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
        await _set_node_state(session_factory, run_id, node.id, status="done", total=1, done=1, failed=0)
        return  # 断点续跑：单元已完成；补写状态以修复「单元已写、状态未写」的崩溃窗口
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
        return await nodes.apply_operations_with_agent(
            inputs, cfg.get("operations", []), seed=cfg.get("seed"))
    if node.type == "output":
        if cfg.get("save_as_dataset"):
            async with session_factory() as s:
                await create_dataset(s, user_id, cfg.get("dataset_name", "运行结果"),
                                     inputs, source="run")
        return inputs
    raise ValueError(f"未知节点类型: {node.type}")


async def _run_llm_node(session_factory, run_id, user_id, node: Node, inputs,
                        user_sem, cancel_event):
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

    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem)
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "",
                                  usage=usage)
                done_count += 1
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                failed_count += 1
            await _set_node_state(session_factory, run_id, node.id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
    if not cancel_event.is_set():
        await _set_node_state(session_factory, run_id, node.id, status="done",
                              total=total, done=done_count, failed=failed_count)


async def _run_qc_node(session_factory, run_id, user_id, graph: Graph, node: Node, inputs,
                       user_sem, cancel_event):
    """质检节点：规则判定每行通过/不通过；不通过的行带原因经 rescan 回扫边的 LLM 重新生成，
    最多 max_rounds 轮，仍不通过的行丢弃。仅持久化本节点最终输出（含各轮 token 汇总）。"""
    cfg = node.config
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, status="done", total=1, done=1, failed=0)
        return
    await _set_node_state(session_factory, run_id, node.id, status="running", total=1, done=0, failed=0)
    try:
        target_id = next((e["target"] for e in graph.edges
                          if e["kind"] == "rescan" and e["source"] == node.id), None)
        passed, failed = nodes.qc_split(inputs, cfg)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        rounds = 0
        if target_id is not None and failed:
            tgt = next(n for n in graph.nodes if n.id == target_id)
            async with session_factory() as s:
                mc = await s.get(ModelConfig, tgt.config.get("model_config_id"))
            if mc is None or mc.user_id != user_id:
                raise ValueError(f"回扫目标 {target_id}: 模型配置不存在")
            sem = asyncio.Semaphore(tgt.config.get("concurrency", 4))

            async def regen(row):
                async with sem:
                    return await nodes.run_llm_synth_row(tgt.config, row, mc, user_sem)

            while failed and rounds < cfg.get("max_rounds", 3) and not cancel_event.is_set():
                rounds += 1
                regenerated: list[dict] = []
                for out_rows, u in await asyncio.gather(*[regen(r) for r in failed]):
                    usage["prompt_tokens"] += u["prompt_tokens"]
                    usage["completion_tokens"] += u["completion_tokens"]
                    regenerated.extend(out_rows)
                fresh_pass, failed = nodes.qc_split(regenerated, cfg)
                passed.extend(fresh_pass)
                await _set_node_state(session_factory, run_id, node.id, status="running",
                                      total=1, done=0, failed=len(failed))
        if cancel_event.is_set():
            return
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, status="failed", total=1, done=0, failed=1)
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", passed, "",
                      usage=usage, qc_round=rounds)
    await _set_node_state(session_factory, run_id, node.id, status="done", total=1, done=1, failed=0)
