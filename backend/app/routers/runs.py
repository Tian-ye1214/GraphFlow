import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session, get_session_factory
from app.events import publish
from app.engine.graph import GraphError, descendants, parse_graph, validate_graph
from app.engine.manager import manager
from app.models import (Dataset, ModelConfig, Run, RunNodeState, RunRow, User,
                        Workflow, WorkflowVersion)
from app.routers.workflows import get_owned_workflow
from app.services.export import export_rows

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunCreate(BaseModel):
    workflow_id: int


async def _get_owned_run(run_id: int, user: User, session: AsyncSession) -> Run:
    run = await session.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="运行不存在")
    return run


@router.post("")
async def create_run(body: RunCreate, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(body.workflow_id, user, session)
    graph = parse_graph(wf.graph_json)
    try:
        validate_graph(graph)
        if not graph.nodes:
            raise GraphError("工作流为空")
    except GraphError as e:
        raise HTTPException(status_code=422, detail=str(e))
    for n in graph.nodes:  # 资源归属校验（会话隔离）
        if n.type == "input":
            for ds_id in n.config.get("dataset_ids", []):
                ds = await session.get(Dataset, ds_id)
                if ds is None or ds.user_id != user.id:
                    raise HTTPException(status_code=422, detail=f"节点 {n.id}: 数据集不存在")
        if n.type == "llm_synth":
            mc = await session.get(ModelConfig, n.config.get("model_config_id"))
            if mc is None or mc.user_id != user.id:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择有效的模型配置")
    max_ver = (await session.execute(select(func.max(WorkflowVersion.version)).where(
        WorkflowVersion.workflow_id == wf.id))).scalar() or 0
    ver = WorkflowVersion(workflow_id=wf.id, version=max_ver + 1, graph_json=wf.graph_json)
    session.add(ver)
    await session.flush()
    run = Run(user_id=user.id, workflow_id=wf.id, workflow_version_id=ver.id)
    session.add(run)
    await session.commit()
    manager.submit(run.id, user.id, user.max_llm_concurrency, get_session_factory())
    publish(user.id, "run", run.id)
    return {"id": run.id, "status": run.status}


def _run_out(run: Run, workflow_name: str = "") -> dict:
    return {
        "id": run.id, "workflow_id": run.workflow_id, "workflow_name": workflow_name,
        "status": run.status, "error": run.error, "stats": json.loads(run.stats_json),
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("")
async def list_runs(workflow_id: int | None = None, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    stmt = (select(Run, Workflow.name).join(Workflow, Run.workflow_id == Workflow.id)
            .where(Run.user_id == user.id).order_by(Run.id.desc()))
    if workflow_id is not None:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    rows = (await session.execute(stmt)).all()
    return [_run_out(run, name) for run, name in rows]


@router.get("/{run_id}")
async def run_detail(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    states = (await session.execute(
        select(RunNodeState).where(RunNodeState.run_id == run.id))).scalars().all()
    return {**_run_out(run, wf.name if wf else ""), "graph": json.loads(ver.graph_json),
            "node_states": [{"node_id": s.node_id, "status": s.status, "total": s.total,
                             "done": s.done, "failed": s.failed} for s in states]}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"当前状态 {run.status} 不可取消")
    manager.cancel(run.id)
    publish(user.id, "run", run.id)
    return {"ok": True}


@router.post("/{run_id}/rerun-failed")
async def rerun_failed(run_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="运行尚未结束")
    failed_nodes = (await session.execute(
        select(RunRow.node_id).where(RunRow.run_id == run.id, RunRow.status == "failed")
        .distinct())).scalars().all()
    if not failed_nodes:
        raise HTTPException(status_code=409, detail="没有失败行")
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    graph = parse_graph(ver.graph_json)
    reset_targets: set[str] = set()
    for nid in failed_nodes:
        reset_targets |= descendants(graph, nid)
    await session.execute(update(RunRow).where(
        RunRow.run_id == run.id, RunRow.status == "failed"
    ).values(status="pending", error=""))
    if reset_targets:
        await session.execute(sa_delete(RunRow).where(
            RunRow.run_id == run.id, RunRow.node_id.in_(reset_targets)))
        await session.execute(sa_delete(RunNodeState).where(
            RunNodeState.run_id == run.id, RunNodeState.node_id.in_(reset_targets)))
    run.status = "queued"
    run.error = ""
    run.finished_at = None
    await session.commit()
    manager.submit(run.id, user.id, user.max_llm_concurrency, get_session_factory())
    publish(user.id, "run", run.id)
    return {"ok": True}


def _flatten(recs: list[RunRow]) -> list[dict]:
    rows: list[dict] = []
    for r in recs:
        rows.extend(json.loads(r.data_json))
    return rows


@router.get("/{run_id}/rows")
async def run_rows(run_id: int, node_id: str, status: str = "done",
                   page: int = 1, page_size: int = 20,
                   user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    base = (RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.status == status)
    total = (await session.execute(
        select(func.count()).select_from(RunRow).where(*base))).scalar()
    recs = (await session.execute(
        select(RunRow).where(*base).order_by(RunRow.row_idx)
        .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    if status == "failed":
        return {"total": total, "rows": [
            {"row_idx": r.row_idx, "error": r.error, "attempt": r.attempt} for r in recs]}
    return {"total": total, "rows": _flatten(recs)}


@router.get("/{run_id}/export")
async def export_run(run_id: int, node_id: str | None = None,
                     format: Literal["jsonl", "csv", "xlsx"] = "jsonl",
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    graph = parse_graph(ver.graph_json)
    if node_id is None:
        outputs = [n for n in graph.nodes if n.type == "output"]
        if not outputs:
            raise HTTPException(status_code=422, detail="工作流没有输出节点")
        node_id = outputs[0].id
    recs = (await session.execute(
        select(RunRow).where(RunRow.run_id == run.id, RunRow.node_id == node_id,
                             RunRow.status == "done").order_by(RunRow.row_idx))).scalars().all()
    filename = f"run{run.id}_{Path(node_id).name}.{format}"
    path = await asyncio.to_thread(
        export_rows, _flatten(recs), format, settings.data_dir / "exports" / filename)
    return FileResponse(path, filename=filename)
