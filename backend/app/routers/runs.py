import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session, get_session_factory
from app.events import publish
from app.engine.graph import GraphError, descendants, parse_graph, validate_graph
from app.engine.manager import manager
from app.models import (Dataset, ModelCallLog, ModelConfig, QcFailure, QcMetric, Run, RunLog,
                        RunNodeState, RunRow, User, Workflow, WorkflowVersion)
from app.routers.workflows import get_owned_workflow
from app.services.export import export_rows
from app.services.run_service import (purge_run_rows, unlink_run_exports,
                                      validate_graph_resource_ownership)

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
    try:  # 资源归属校验（会话隔离）——与 dry_run 共用单点，防校验漂移
        await validate_graph_resource_ownership(session, graph, user.id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
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


@router.get("/{run_id}/logs")
async def run_logs(run_id: int, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    logs = (await session.execute(
        select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id))).scalars().all()
    return [{"created_at": l.created_at.isoformat(), "node_id": l.node_id,
             "level": l.level, "message": l.message} for l in logs]


@router.get("/{run_id}/model-logs")
async def run_model_logs(run_id: int, node_id: str | None = None, source: str | None = None,
                         limit: int = 200, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    from app.routers.model_logs import _out
    stmt = select(ModelCallLog).where(ModelCallLog.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(ModelCallLog.node_id == node_id)
    if source is not None:
        stmt = stmt.where(ModelCallLog.source == source)
    rows = (await session.execute(
        stmt.order_by(ModelCallLog.id.desc()).limit(min(max(limit, 0), 500)))).scalars().all()
    return [_out(r) for r in rows]


@router.get("/{run_id}/qc-metrics")
async def run_qc_metrics(run_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    rows = (await session.execute(
        select(QcMetric).where(QcMetric.run_id == run_id).order_by(QcMetric.id))).scalars().all()
    return [{"node_id": m.node_id, "total": m.total, "first_round_pass": m.first_round_pass,
             "first_round_rate": (m.first_round_pass / m.total) if m.total else 0.0} for m in rows]


@router.get("/{run_id}/qc-failures")
async def run_qc_failures(run_id: int, node_id: str | None = None, limit: int = 200,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    stmt = select(QcFailure).where(QcFailure.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(QcFailure.node_id == node_id)
    rows = (await session.execute(stmt.order_by(QcFailure.id).limit(limit))).scalars().all()
    return [{"node_id": f.node_id, "sample": json.loads(f.sample_json),
             "reasons": json.loads(f.reasons_json), "created_at": f.created_at.isoformat()}
            for f in rows]


@router.get("/{run_id}/qc-failures.jsonl")
async def run_qc_failures_jsonl(run_id: int, node_id: str | None = None,
                                user: User = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """最终失败样本全量导出为 jsonl：每行 = 样本字段 + 各判定模型平铺 _qc_model_i/_qc_model_i_reason。"""
    await _get_owned_run(run_id, user, session)
    stmt = select(QcFailure).where(QcFailure.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(QcFailure.node_id == node_id)
    rows = (await session.execute(stmt.order_by(QcFailure.id))).scalars().all()
    lines = []
    for f in rows:
        rec = json.loads(f.sample_json)
        for i, pm in enumerate(json.loads(f.reasons_json), start=1):
            rec[f"_qc_model_{i}"] = pm.get("status", "")
            rec[f"_qc_model_{i}_reason"] = pm.get("reason", "")
        lines.append(json.dumps(rec, ensure_ascii=False))
    return Response(content="\n".join(lines), media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="run{run_id}_qc_failures.jsonl"'})


@router.delete("")
async def delete_all_runs(user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    runs = (await session.execute(select(Run).where(
        Run.user_id == user.id, Run.status.notin_(("queued", "running"))))).scalars().all()
    run_ids = [r.id for r in runs]
    ver_ids = [r.workflow_version_id for r in runs]
    if run_ids:
        await purge_run_rows(session, run_ids, version_ids=ver_ids)
        await session.commit()
        unlink_run_exports(run_ids, settings.data_dir)
    return {"deleted": len(run_ids)}


@router.delete("/{run_id}")
async def delete_run(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status in ("queued", "running"):
        raise HTTPException(status_code=409, detail="运行中，请先取消再删除")
    ver_id = run.workflow_version_id
    await purge_run_rows(session, [run_id], version_ids=[ver_id])
    await session.commit()
    unlink_run_exports([run_id], settings.data_dir)
    publish(user.id, "run", run_id)
    return {"ok": True}


@router.post("/{run_id}/restore")
async def restore_run_version(run_id: int, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    wf.graph_json = ver.graph_json
    await session.commit()
    publish(user.id, "workflow", wf.id)
    return {"ok": True}


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
    # 将重算的节点（失败节点本身 + 重置的下游）清掉旧 QC 指标/失败样本，否则重算会再 INSERT
    # 一条，导致 qc-metrics 同节点重复、first_round_rate（目标模式标尺）被双算。
    affected = set(failed_nodes) | reset_targets
    for Model in (QcMetric, QcFailure):
        await session.execute(sa_delete(Model).where(
            Model.run_id == run.id, Model.node_id.in_(affected)))
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
