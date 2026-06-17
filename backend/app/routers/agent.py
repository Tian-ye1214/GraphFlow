import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import codegen as codegen_mod
from app.agent.codegen import gather_upstream_columns, generate_code
from app.agent.turns import session_dir, turn_manager
from app.auth import get_current_user, make_session_cookie
from app.db import get_session
from app.engine.graph import parse_graph
from app.events import publish
from app.models import AgentMessage, AgentSession, ModelConfig, User, Workflow
from app.services.run_service import workflow_has_qc

router = APIRouter(prefix="/api/agent", tags=["agent"])

ROLES = ("coordinator", "manager", "worker")


class SessionIn(BaseModel):
    model_config_id: int | None = None
    models: dict[str, int] | None = None


class MessageIn(BaseModel):
    text: str


def _out(sess: AgentSession) -> dict:
    return {"id": sess.id, "title": sess.title, "status": sess.status,
            "models": json.loads(sess.models_json),
            "created_at": sess.created_at.isoformat(),
            "updated_at": sess.updated_at.isoformat()}


async def _get_owned(sid: int, user: User, session: AsyncSession) -> AgentSession:
    sess = await session.get(AgentSession, sid)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


async def _check_models(models: dict, user: User, session: AsyncSession) -> None:
    roles = ["coordinator", "manager", "worker"]
    if "compactor" in models:
        roles.append("compactor")
    for role in roles:
        mc = await session.get(ModelConfig, models.get(role) or 0)
        if mc is None or mc.user_id != user.id:
            raise HTTPException(status_code=422, detail=f"角色 {role} 的模型配置无效")


@router.post("/sessions")
async def create_session(body: SessionIn, request: Request,
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    models = body.models or {r: body.model_config_id for r in ROLES}
    models.setdefault("compactor", models["coordinator"])
    await _check_models(models, user, session)
    seq = (await session.scalar(select(func.count()).select_from(AgentSession)
                                .where(AgentSession.user_id == user.id))) + 1
    sess = AgentSession(user_id=user.id, title=f"会话 {seq}", models_json=json.dumps(models))
    session.add(sess)
    await session.commit()
    wd = session_dir(user.username, sess.id)
    wd.mkdir(parents=True, exist_ok=True)
    server = str(request.base_url).rstrip("/")
    (wd / "cli.json").write_text(
        json.dumps({"server": server, "cookie": make_session_cookie(user.id)}),
        encoding="utf-8")
    return _out(sess)


@router.get("/sessions")
async def list_sessions(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(AgentSession).where(AgentSession.user_id == user.id)
        .order_by(AgentSession.id.desc()))).scalars().all()
    return [_out(s) for s in rows]


@router.get("/sessions/{sid}")
async def get_session_detail(sid: int, user: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    msgs = (await session.execute(
        select(AgentMessage).where(AgentMessage.session_id == sid)
        .order_by(AgentMessage.id))).scalars().all()
    return {**_out(sess), "messages": [
        {"id": m.id, "role": m.role, "content": json.loads(m.content_json),
         "created_at": m.created_at.isoformat()} for m in msgs]}


@router.post("/sessions/{sid}/messages")
async def post_message(sid: int, body: MessageIn,
                       user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    if sess.status == "running":
        raise HTTPException(status_code=409, detail="回合进行中")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空")
    await _check_models(json.loads(sess.models_json), user, session)
    first = (await session.scalar(select(func.count()).select_from(AgentMessage)
                                  .where(AgentMessage.session_id == sid))) == 0
    session.add(AgentMessage(session_id=sid, role="user",
                             content_json=json.dumps({"text": text}, ensure_ascii=False)))
    if first:  # 首条消息把"会话 N"占位标题改为消息预览
        sess.title = text[:30]
    sess.status = "running"
    await session.commit()
    publish(user.id, "agent", sid, kind="message")
    turn_manager.submit(sid, user.id, text)
    return {"ok": True}


@router.post("/sessions/{sid}/stop")
async def stop_session(sid: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    await _get_owned(sid, user, session)
    turn_manager.request_stop(sid)
    return {"ok": True}


class GoalIn(BaseModel):
    workflow_id: int
    goal_text: str


@router.post("/sessions/{sid}/goal")
async def start_goal(sid: int, body: GoalIn, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    if sess.status == "running":
        raise HTTPException(status_code=409, detail="回合进行中")
    text = body.goal_text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="目标不能为空")
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    if not workflow_has_qc(parse_graph(wf.graph_json)):
        raise HTTPException(status_code=422, detail="目标工作流需包含质检节点才能度量首轮质检通过率")
    await _check_models(json.loads(sess.models_json), user, session)
    session.add(AgentMessage(session_id=sid, role="user",
                             content_json=json.dumps({"text": f"[目标模式] {text}"}, ensure_ascii=False)))
    sess.status = "running"
    await session.commit()
    publish(user.id, "agent", sid, kind="message")
    turn_manager.submit_goal(sid, user.id, body.workflow_id, text)
    return {"ok": True}


@router.delete("/sessions")
async def delete_all_sessions(user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    sessions = (await session.execute(select(AgentSession).where(
        AgentSession.user_id == user.id))).scalars().all()
    sids = [s.id for s in sessions]
    for sid in sids:
        turn_manager.cancel(sid)
    if sids:
        await session.execute(sa_delete(AgentMessage).where(AgentMessage.session_id.in_(sids)))
        await session.execute(sa_delete(AgentSession).where(AgentSession.id.in_(sids)))
        await session.commit()
        for sid in sids:
            shutil.rmtree(session_dir(user.username, sid), ignore_errors=True)
    return {"deleted": len(sids)}


@router.delete("/sessions/{sid}")
async def delete_session(sid: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned(sid, user, session)
    turn_manager.cancel(sid)
    await session.execute(sa_delete(AgentMessage).where(AgentMessage.session_id == sid))
    await session.execute(sa_delete(AgentSession).where(AgentSession.id == sid))
    await session.commit()
    shutil.rmtree(session_dir(user.username, sid), ignore_errors=True)
    return {"ok": True}


class CodegenIn(BaseModel):
    workflow_id: int
    node_id: str
    instruction: str
    model_config_id: int
    current_code: str | None = None


@router.post("/codegen")
async def codegen(body: CodegenIn, user: User = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    mc = await session.get(ModelConfig, body.model_config_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=422, detail="模型配置无效")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="指令不能为空")
    columns, source = await gather_upstream_columns(session, body.workflow_id, body.node_id, user.id)
    result = await generate_code(mc, body.instruction, columns, current_code=body.current_code or "")
    return {"code": result["code"], "output_columns": result["output_columns"],
            "columns": columns, "sample_source": source}


class NodeAssistIn(BaseModel):
    workflow_id: int
    node_id: str
    node_type: str
    instruction: str
    model_config_id: int
    current_config: dict | None = None


@router.post("/node-assist")
async def node_assist(body: NodeAssistIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    if body.node_type not in ("llm_synth", "qc"):
        raise HTTPException(status_code=422, detail="该节点类型不支持助手")
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    mc = await session.get(ModelConfig, body.model_config_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=422, detail="模型配置无效")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="指令不能为空")
    columns, source = await gather_upstream_columns(session, body.workflow_id, body.node_id, user.id)
    config = await codegen_mod.generate_node_config(
        mc, body.node_type, body.instruction, columns, current_config=body.current_config)
    return {"config": config, "sample_source": source}
