import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.events import publish
from app.models import User, Workflow

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
    await session.delete(wf)
    await session.commit()
    publish(user.id, "workflow", wf_id)
    return {"ok": True}
