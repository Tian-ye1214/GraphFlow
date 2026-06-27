import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import Prompt, PromptVersion, User, Workflow
from app.services import prompt_service
from app.services.prompt_service import extract_vars, _latest

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptIn(BaseModel):
    name: str
    description: str = ""
    body: str = ""


async def _get_owned(pid: int, user: User, session: AsyncSession) -> Prompt:
    p = await session.get(Prompt, pid)
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return p


async def _used_by(session: AsyncSession, user: User, pid: int) -> list[dict]:
    wfs = (await session.execute(select(Workflow).where(Workflow.user_id == user.id))).scalars().all()
    out = []
    for wf in wfs:
        graph = json.loads(wf.graph_json)
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        for node in nodes:
            # 草稿图可存畸形节点（节点非 dict / 缺 id / config 非 dict）——跳过，勿让只读 prompt 端点 500
            if not isinstance(node, dict):
                continue
            cfg = node.get("config")
            if not isinstance(cfg, dict):
                continue
            for slot in ("system_prompt", "user_prompt"):
                if cfg.get(f"{slot}_ref") == pid:
                    out.append({"workflow_id": wf.id, "workflow_name": wf.name,
                                "node_id": node.get("id"), "slot": slot})
    return out


async def _detail(session: AsyncSession, user: User, pid: int) -> dict:
    p = await _get_owned(pid, user, session)
    vers = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version))).scalars().all()
    cur = vers[-1]
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "current": {"version": cur.version, "body": cur.body, "variables": json.loads(cur.variables_json)},
        "versions": [{"version": v.version, "created_at": v.created_at.isoformat()} for v in vers],
        "used_by": await _used_by(session, user, pid),
    }


@router.get("")
async def list_prompts(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ps = (await session.execute(select(Prompt).where(Prompt.user_id == user.id).order_by(Prompt.id))).scalars().all()
    out = []
    for p in ps:
        cur = await _latest(session, p.id)
        out.append({"id": p.id, "name": p.name, "description": p.description,
                    "latest_version": cur.version, "variables": json.loads(cur.variables_json)})
    return out


@router.post("")
async def create_prompt(body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    pid = await prompt_service.create_prompt(session, user.id, name=body.name,
                                             description=body.description, body=body.body)
    return await _detail(session, user, pid)


@router.get("/{pid}")
async def get_prompt(pid: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    return await _detail(session, user, pid)


@router.put("/{pid}")
async def update_prompt(pid: int, body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await prompt_service.update_prompt(session, p, name=body.name, description=body.description, body=body.body)
    return await _detail(session, user, pid)


@router.delete("/{pid}")
async def delete_prompt(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await prompt_service.delete_prompt(session, p)
    return {"ok": True}


class RollbackIn(BaseModel):
    version: int


class DuplicateIn(BaseModel):
    name: str | None = None


@router.get("/{pid}/versions")
async def list_versions(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    await _get_owned(pid, user, session)
    vers = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version))).scalars().all()
    return [{"version": v.version, "body": v.body, "variables": json.loads(v.variables_json),
             "created_at": v.created_at.isoformat()} for v in vers]


@router.post("/{pid}/rollback")
async def rollback_prompt(pid: int, body: RollbackIn, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_owned(pid, user, session)
    if not await prompt_service.rollback_prompt(session, pid, body.version):
        raise HTTPException(status_code=404, detail="版本不存在")
    return await _detail(session, user, pid)


@router.post("/{pid}/duplicate")
async def duplicate_prompt(pid: int, body: DuplicateIn, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    src = await _get_owned(pid, user, session)
    new_id = await prompt_service.duplicate_prompt(session, src, body.name)
    return await _detail(session, user, new_id)
