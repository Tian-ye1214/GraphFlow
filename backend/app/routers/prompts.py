import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.engine.nodes import TEMPLATE_RE   # 复用引擎占位符正则，保证抽取与渲染一致
from app.events import publish
from app.models import Prompt, PromptVersion, User

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptIn(BaseModel):
    name: str
    description: str = ""
    body: str = ""


def extract_vars(body: str) -> list[str]:
    return sorted({m.group(1) for m in TEMPLATE_RE.finditer(body or "")})


async def _get_owned(pid: int, user: User, session: AsyncSession) -> Prompt:
    p = await session.get(Prompt, pid)
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return p


async def _latest(session: AsyncSession, pid: int) -> PromptVersion:
    return (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version.desc()).limit(1))).scalar_one()


async def _detail(session: AsyncSession, user: User, pid: int) -> dict:
    p = await _get_owned(pid, user, session)
    vers = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version))).scalars().all()
    cur = vers[-1]
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "current": {"version": cur.version, "body": cur.body, "variables": json.loads(cur.variables_json)},
        "versions": [{"version": v.version, "created_at": v.created_at.isoformat()} for v in vers],
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
    p = Prompt(user_id=user.id, name=body.name, description=body.description)
    session.add(p)
    await session.flush()
    session.add(PromptVersion(prompt_id=p.id, version=1, body=body.body,
                              variables_json=json.dumps(extract_vars(body.body), ensure_ascii=False)))
    await session.commit()
    publish(user.id, "prompt", p.id)
    return await _detail(session, user, p.id)


@router.get("/{pid}")
async def get_prompt(pid: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    return await _detail(session, user, pid)


@router.put("/{pid}")
async def update_prompt(pid: int, body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    p.name, p.description = body.name, body.description
    cur = await _latest(session, pid)
    if body.body != cur.body:   # 仅正文变化才追加新版本；名称/描述是元数据，原地改
        session.add(PromptVersion(prompt_id=pid, version=cur.version + 1, body=body.body,
                                  variables_json=json.dumps(extract_vars(body.body), ensure_ascii=False)))
    await session.commit()
    publish(user.id, "prompt", pid)
    return await _detail(session, user, pid)


@router.delete("/{pid}")
async def delete_prompt(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await session.execute(delete(PromptVersion).where(PromptVersion.prompt_id == pid))
    await session.delete(p)
    await session.commit()
    publish(user.id, "prompt", pid)
    return {"ok": True}
