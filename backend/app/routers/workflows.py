import json
import os
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.events import publish
from app.models import ModelCallLog, Run, User, Workflow, WorkflowVersion
from app.routers.datasets import _safe_filename
from app.services.run_service import purge_run_rows, unlink_run_exports
from app.services.workflow_package import export_package, import_package, PackageError

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
    # 草稿态图（有环/悬空边/脏 config 形状）属正常编辑中间态：WorkflowUpdate.graph 只校验顶层 dict，
    # 节点内部可存任意脏值（config 非 dict / dataset_ids 非 list / rename mapping 非 dict / op 非 dict），
    # 计算列血缘时会抛 AttributeError/TypeError/KeyError——统一降级 422 而非 500（对齐 GraphError 既有契约）。
    try:
        graph = parse_graph(wf.graph_json)
        validate_graph(graph)
        dataset_cols = await resolve_dataset_cols(session, graph, user.id)
        return propagate_columns(graph, dataset_cols)
    except GraphError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except (AttributeError, TypeError, KeyError) as e:
        raise HTTPException(status_code=422, detail=f"草稿态图结构非法，无法计算列血缘: {e}")


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
    # 级联清理子数据，否则版本/运行/行/日志/指标全成孤儿，run 还会脱离列表泄漏。
    # 版本按 workflow_id 整体删（含没有 run 的草稿版本），故 purge 不带 version_ids。
    await purge_run_rows(session, run_ids)
    # 节点助手类日志（无 run、按 workflow 归属）一并清
    await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.workflow_id == wf.id))
    await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id))
    await session.delete(wf)
    await session.commit()
    unlink_run_exports(run_ids, settings.data_dir)
    publish(user.id, "workflow", wf_id)
    return {"ok": True}


@router.get("/{wf_id}/export")
async def export_workflow(wf_id: int, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    fd, tmp = tempfile.mkstemp(suffix=".gfpkg", dir=settings.data_dir)
    os.close(fd)
    try:
        await export_package(session, wf, tmp)
    except Exception:
        os.unlink(tmp)
        raise
    safe = _safe_filename(wf.name)
    ascii_name = (safe.encode("ascii", "ignore").decode().strip() or "workflow") + ".gfpkg"
    disp = (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(safe + '.gfpkg')}")
    return FileResponse(tmp, media_type="application/zip",
                        headers={"Content-Disposition": disp},
                        background=BackgroundTask(os.unlink, tmp))


@router.post("/import")
async def import_workflow(file: UploadFile, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    fd, tmp = tempfile.mkstemp(suffix=".gfpkg", dir=settings.data_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        try:
            wf_out, report = await import_package(session, tmp, user.id)
        except (PackageError, GraphError) as e:
            raise HTTPException(status_code=422, detail=str(e))
    finally:
        os.unlink(tmp)
    publish(user.id, "workflow", wf_out["id"])
    return {"workflow": wf_out, "report": report}
