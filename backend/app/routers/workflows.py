import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.events import publish
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    name: str


class WorkflowUpdate(BaseModel):
    name: str | None = None
    graph: dict | None = None


def _out(wf: Workflow) -> dict:
    return {"id": wf.id, "name": wf.name, "graph": json.loads(wf.graph_json),
            "updated_at": wf.updated_at.isoformat()}


# 不带下划线前缀：runs 路由（Task 12）会导入复用
async def get_owned_workflow(wf_id: int, user: User, session: AsyncSession) -> Workflow:
    wf = await session.get(Workflow, wf_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return wf


@router.get("")
async def list_workflows(user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Workflow).where(Workflow.user_id == user.id).order_by(Workflow.updated_at.desc())
    )).scalars().all()
    return [{"id": w.id, "name": w.name, "updated_at": w.updated_at.isoformat()} for w in rows]


@router.post("")
async def create_workflow(body: WorkflowCreate, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = Workflow(user_id=user.id, name=body.name)
    session.add(wf)
    await session.commit()
    publish(user.id, "workflow", wf.id)
    return _out(wf)


@router.get("/{wf_id}")
async def get_workflow(wf_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    return _out(await get_owned_workflow(wf_id, user, session))


@router.get("/{wf_id}/columns")
async def workflow_columns(wf_id: int, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    graph = parse_graph(wf.graph_json)
    try:  # 草稿态图（有环/悬空边）属正常编辑中间态，应给 422 而非 500（对齐 create_run）
        validate_graph(graph)
    except GraphError as e:
        raise HTTPException(status_code=422, detail=str(e))
    dataset_cols = await resolve_dataset_cols(session, graph, user.id)
    return propagate_columns(graph, dataset_cols)


@router.put("/{wf_id}")
async def update_workflow(wf_id: int, body: WorkflowUpdate, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    if body.name is not None:
        wf.name = body.name
    if body.graph is not None:
        wf.graph_json = json.dumps(body.graph, ensure_ascii=False)
    await session.commit()
    publish(user.id, "workflow", wf.id)
    return _out(wf)


@router.delete("/{wf_id}")
async def delete_workflow(wf_id: int, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    runs = (await session.execute(
        select(Run.id, Run.status).where(Run.workflow_id == wf.id))).all()
    if any(st in ("queued", "running") for _, st in runs):
        raise HTTPException(status_code=409, detail="存在运行中的任务，请先取消再删除")
    run_ids = [rid for rid, _ in runs]
    if run_ids:  # 级联清理子数据，否则版本/运行/行/日志/指标全成孤儿，run 还会脱离列表泄漏
        for Model in (RunRow, RunNodeState, RunLog, QcMetric, QcFailure, ModelCallLog):
            await session.execute(sa_delete(Model).where(Model.run_id.in_(run_ids)))
        await session.execute(sa_delete(Run).where(Run.workflow_id == wf.id))
    # 节点助手类日志（无 run、按 workflow 归属）一并清
    await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.workflow_id == wf.id))
    await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id))
    await session.delete(wf)
    await session.commit()
    for rid in run_ids:
        for p in (settings.data_dir / "exports").glob(f"run{rid}_*"):
            p.unlink(missing_ok=True)
    publish(user.id, "workflow", wf_id)
    return {"ok": True}
