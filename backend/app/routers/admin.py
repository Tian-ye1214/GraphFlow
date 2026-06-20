import shutil

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.turns import _safe
from app.auth import ACT_AS_COOKIE, COOKIE_MAX_AGE, make_act_as_cookie, require_admin
from app.config import settings
from app.db import get_session
from app.models import (AgentMessage, AgentSession, Dataset, DatasetRow, ModelCallLog, ModelConfig,
                        Prompt, PromptVersion, QcFailure, QcMetric, Run, RunLog, RunNodeState,
                        RunRow, User, Workflow, WorkflowVersion)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_row(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "is_admin": u.is_admin, "created_at": u.created_at.isoformat()}


class ActAsIn(BaseModel):
    user_id: int | None


@router.post("/act-as")
async def act_as(body: ActAsIn, response: Response, admin: User = Depends(require_admin),
                 session: AsyncSession = Depends(get_session)):
    if body.user_id is None:
        response.delete_cookie(ACT_AS_COOKIE)
        return _user_row(admin)
    target = await session.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    response.set_cookie(ACT_AS_COOKIE, make_act_as_cookie(target.id),
                        httponly=True, max_age=COOKIE_MAX_AGE)
    return _user_row(target)


@router.get("/users")
async def list_users(admin: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    users = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return [_user_row(u) for u in users]


class UserCreate(BaseModel):
    username: str
    display_name: str = ""


@router.post("/users")
async def create_user(body: UserCreate, admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    if (await session.execute(select(User).where(User.username == username))).scalar_one_or_none():
        raise HTTPException(status_code=422, detail="用户名已存在")
    user = User(username=username, display_name=body.display_name or username,
                is_admin=username in settings.admin_user_set)
    session.add(user)
    await session.commit()
    return _user_row(user)


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="不能删除自己")
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = target.username
    # --- 收集子资源 ID（级联用）---
    ds_ids = (await session.execute(
        select(Dataset.id).where(Dataset.user_id == user_id))).scalars().all()
    run_ids = (await session.execute(
        select(Run.id).where(Run.user_id == user_id))).scalars().all()
    wf_ids = (await session.execute(
        select(Workflow.id).where(Workflow.user_id == user_id))).scalars().all()
    sess_ids = (await session.execute(
        select(AgentSession.id).where(AgentSession.user_id == user_id))).scalars().all()
    prompt_ids = (await session.execute(
        select(Prompt.id).where(Prompt.user_id == user_id))).scalars().all()
    # --- 级联删除：子表 → 父表 → User ---
    # 模型调用日志(request_json=完整提示词、response_json=模型回复正文)：按 user_id 为主，
    # run/workflow/session 关联兜底 user_id 未填的历史行。须先于 Run/Workflow/AgentSession 删（有外键指向三者）。
    mcl_conds = [ModelCallLog.user_id == user_id]
    if run_ids:
        mcl_conds.append(ModelCallLog.run_id.in_(run_ids))
    if wf_ids:
        mcl_conds.append(ModelCallLog.workflow_id.in_(wf_ids))
    if sess_ids:
        mcl_conds.append(ModelCallLog.session_id.in_(sess_ids))
    await session.execute(sa_delete(ModelCallLog).where(or_(*mcl_conds)))
    if ds_ids:
        await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id.in_(ds_ids)))
    await session.execute(sa_delete(Dataset).where(Dataset.user_id == user_id))
    await session.execute(sa_delete(ModelConfig).where(ModelConfig.user_id == user_id))
    if run_ids:
        await session.execute(sa_delete(RunRow).where(RunRow.run_id.in_(run_ids)))
        await session.execute(sa_delete(RunNodeState).where(RunNodeState.run_id.in_(run_ids)))
        await session.execute(sa_delete(RunLog).where(RunLog.run_id.in_(run_ids)))
        await session.execute(sa_delete(QcMetric).where(QcMetric.run_id.in_(run_ids)))
        await session.execute(sa_delete(QcFailure).where(QcFailure.run_id.in_(run_ids)))
    await session.execute(sa_delete(Run).where(Run.user_id == user_id))
    if wf_ids:
        await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(wf_ids)))
    await session.execute(sa_delete(Workflow).where(Workflow.user_id == user_id))
    if sess_ids:
        await session.execute(sa_delete(AgentMessage).where(AgentMessage.session_id.in_(sess_ids)))
    await session.execute(sa_delete(AgentSession).where(AgentSession.user_id == user_id))
    if prompt_ids:
        await session.execute(sa_delete(PromptVersion).where(PromptVersion.prompt_id.in_(prompt_ids)))
    await session.execute(sa_delete(Prompt).where(Prompt.user_id == user_id))
    await session.execute(sa_delete(User).where(User.id == user_id))
    await session.commit()
    # 该用户上传的数据集文件都在 uploads/<user_id>/，整目录删除即可（运行结果数据集 file_path 为空）
    shutil.rmtree(settings.data_dir / "uploads" / str(user_id), ignore_errors=True)
    shutil.rmtree(settings.data_dir / "agent" / _safe(username), ignore_errors=True)
    for rid in run_ids:
        for p in (settings.data_dir / "exports").glob(f"run{rid}_*"):
            p.unlink(missing_ok=True)
    return {"ok": True}
