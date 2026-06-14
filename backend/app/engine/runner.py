import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine import nodes
from app.engine.graph import Graph, Node, parse_graph, topo_order, upstream_ids, validate_graph
from app.events import publish
from app.models import (DatasetRow, ModelConfig, QcFailure, QcMetric, Run, RunLog,
                        RunNodeState, RunRow, WorkflowVersion)
from app.routers.datasets import create_dataset


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _cancellable(coro, cancel_event: asyncio.Event):
    """运行 coro，期间若 cancel_event 置位则立刻中止 coro（硬中断）；被中止时抛 CancelledError。
    若 coro 自身完成（含抛业务异常）则正常返回/抛出，不受影响。"""
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise asyncio.CancelledError()


async def _log(session_factory, run_id, node_id, message, level="info"):
    async with session_factory() as s:
        s.add(RunLog(run_id=run_id, node_id=node_id, message=message, level=level))
        await s.commit()


async def _node_counts(session_factory, run_id, node_id) -> tuple[int, int]:
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == node_id))).scalar_one_or_none()
    return (ns.done, ns.failed) if ns else (0, 0)


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
        await _log(session_factory, run_id, "", f"运行失败：{e}", "error")


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
    await _log(session_factory, run_id, "", "运行开始")

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
        await _log(session_factory, run_id, node.id, f"▶ 节点 {node.id} 开始")
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event)
        elif node.type == "qc":
            await _run_qc_node(session_factory, run_id, user_id, graph, node, inputs,
                               user_sem, cancel_event)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        done, failed = await _node_counts(session_factory, run_id, node.id)
        prefix = "✓" if not failed else "✗" if not done else "⚠"
        await _log(session_factory, run_id, node.id,
                   f"{prefix} 节点 {node.id} 完成（done={done} failed={failed}）",
                   "error" if failed else "info")
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
    await _finish(session_factory, run_id, "completed")


async def _finish(session_factory, run_id, status):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        user_id = run.user_id
        sums = (await s.execute(
            select(func.coalesce(func.sum(RunRow.prompt_tokens), 0),
                   func.coalesce(func.sum(RunRow.completion_tokens), 0))
            .where(RunRow.run_id == run_id, RunRow.status == "done"))).one()
        run.stats_json = json.dumps({"prompt_tokens": sums[0], "completion_tokens": sums[1]})
        run.status = status
        run.finished_at = _now()
        await s.commit()
    await _log(session_factory, run_id, "",
               f"运行结束：{status}（prompt={sums[0]} completion={sums[1]}）")
    publish(user_id, "run", run_id)


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


async def _set_node_state(session_factory, run_id, node_id, *, user_id, status, total, done, failed):
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == node_id
        ))).scalar_one_or_none()
        if ns is None:
            ns = RunNodeState(run_id=run_id, node_id=node_id)
            s.add(ns)
        ns.status, ns.total, ns.done, ns.failed = status, total, done, failed
        await s.commit()
    publish(user_id, "run", run_id, kind="progress",
            data={"node_id": node_id, "status": status, "total": total, "done": done, "failed": failed})


async def _run_barrier_node(session_factory, run_id, user_id, node: Node, inputs):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done", total=1, done=1, failed=0)
        return  # 断点续跑：单元已完成；补写状态以修复「单元已写、状态未写」的崩溃窗口
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running", total=1, done=0, failed=0)
    try:
        out = await _barrier_output(session_factory, user_id, node, inputs)
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="failed", total=1, done=0, failed=1)
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", out, "")
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done", total=1, done=1, failed=0)


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
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                          total=total, done=done_count, failed=failed_count)
    todo = [i for i in range(total) if i not in done_idx and i not in failed_idx]
    node_sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await _cancellable(
                    nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem), cancel_event)
            except asyncio.CancelledError:
                return  # 硬中断：在途请求已 abort，该行不落库（保持 pending）
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                failed_count += 1
            else:
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "",
                                  usage=usage)
                done_count += 1
            await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
    if not cancel_event.is_set():
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done",
                              total=total, done=done_count, failed=failed_count)


async def _run_qc_node(session_factory, run_id, user_id, graph: Graph, node: Node, inputs,
                       user_sem, cancel_event):
    """质检节点：N 个判定模型 K-of-N 判定；不通过行带原因经 rescan 回扫重生，最多 max_rounds 轮。
    首轮通过数写 QcMetric；最终仍失败样本写 QcFailure；仅持久化最终通过行。"""
    cfg = node.config
    judge_ids = cfg.get("judge_model_ids") or (
        [cfg["model_config_id"]] if cfg.get("model_config_id") else [])
    pass_k = cfg.get("pass_k", 1)
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
        jmcs = [await s.get(ModelConfig, jid) for jid in judge_ids]
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done",
                              total=len(inputs), done=len(inputs), failed=0)
        return
    if not jmcs or any(m is None or m.user_id != user_id for m in jmcs):
        raise ValueError(f"质检节点 {node.id}: 判定模型配置不存在")
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                          total=len(inputs), done=0, failed=0)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def fold(u):
        usage["prompt_tokens"] += u["prompt_tokens"]
        usage["completion_tokens"] += u["completion_tokens"]

    sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def judge_all(rows):
        async def judge(row):
            async with sem:
                return await _cancellable(
                    nodes.run_qc_judge_row(cfg, row, jmcs, pass_k, user_sem), cancel_event)
        passed_, failed_ = [], []
        for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append(row)
            else:
                failed_.append({**row, "_qc_reason": reason, "_qc_per_model": per_model})
        return passed_, failed_

    try:
        passed, failed = await judge_all(inputs)
        async with session_factory() as s:        # 首轮指标落库
            s.add(QcMetric(run_id=run_id, node_id=node.id,
                           total=len(inputs), first_round_pass=len(passed)))
            await s.commit()
        rounds = 0
        target_id = next((e["target"] for e in graph.edges
                          if e["kind"] == "rescan" and e["source"] == node.id), None)
        if target_id is not None and failed:
            tgt = next(n for n in graph.nodes if n.id == target_id)
            async with session_factory() as s:
                tmc = await s.get(ModelConfig, tgt.config.get("model_config_id"))
            if tmc is None or tmc.user_id != user_id:
                raise ValueError(f"回扫目标 {target_id}: 模型配置不存在")
            rsem = asyncio.Semaphore(tgt.config.get("concurrency", 4))

            async def regen(row):
                async with rsem:
                    return await _cancellable(
                        nodes.run_llm_synth_row(tgt.config, row, tmc, user_sem), cancel_event)

            while failed and rounds < cfg.get("max_rounds", 3):
                rounds += 1
                regenerated: list[dict] = []
                for out_rows, u in await asyncio.gather(*[regen(r) for r in failed]):
                    fold(u)
                    regenerated.extend(out_rows)
                fresh_pass, failed = await judge_all(regenerated)
                passed.extend(fresh_pass)
                await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                                      total=len(inputs), done=len(passed), failed=len(failed))
        if failed:                                # 最终仍失败样本落库
            async with session_factory() as s:
                for fr in failed:
                    sample = nodes.strip_qc_internal(fr)
                    s.add(QcFailure(run_id=run_id, node_id=node.id,
                                    sample_json=json.dumps(sample, ensure_ascii=False),
                                    reasons_json=json.dumps(fr.get("_qc_per_model", []),
                                                            ensure_ascii=False)))
                await s.commit()
    except asyncio.CancelledError:
        return
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="failed",
                              total=len(inputs), done=0, failed=len(inputs))
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", passed, "",
                      usage=usage, qc_round=rounds)
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done",
                          total=len(inputs), done=len(passed), failed=len(inputs) - len(passed))
