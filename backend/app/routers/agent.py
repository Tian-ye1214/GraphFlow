import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic_ai.exceptions import ModelHTTPError
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import codegen as codegen_mod
from app.agent.codegen import gather_upstream_columns, generate_code
from app.agent.data_preview import make_preview_tools
from app.agent.turns import session_dir, turn_manager
from app.auth import get_current_user, make_session_cookie
from app.db import get_session, get_session_factory
from app.engine.graph import parse_graph
from app.events import publish
from app.models import AgentMessage, AgentSession, ModelCallLog, ModelConfig, User, Workflow
from app.services.model_log import log_context
from app.services.run_service import workflow_has_qc
from app.thinking import with_thinking_defaults

router = APIRouter(prefix="/api/agent", tags=["agent"])

ROLES = ("coordinator", "manager", "worker")
AGENT_ROLES = ("coordinator", "manager", "worker", "compactor")


class SessionIn(BaseModel):
    model_config_id: int | None = None
    models: dict[str, int] | None = None
    model_params: dict[str, dict] | None = None


class MessageIn(BaseModel):
    text: str


def _out(sess: AgentSession) -> dict:
    return {"id": sess.id, "title": sess.title, "status": sess.status,
            "models": json.loads(sess.models_json),
            "model_params": json.loads(getattr(sess, "model_params_json", "{}") or "{}"),
            "created_at": sess.created_at.isoformat(),
            "updated_at": sess.updated_at.isoformat()}


def _role_model_params(model_params: dict[str, dict] | None) -> dict[str, dict]:
    return {role: with_thinking_defaults((model_params or {}).get(role)) for role in AGENT_ROLES}


def _raise_model_http_error(exc: ModelHTTPError, mc: ModelConfig) -> None:
    if (getattr(mc, "provider", None) or "openai") == "azure" and exc.status_code in (400, 404):
        azure_mode = (getattr(mc, "azure_api_mode", None) or "legacy").lower()
        if azure_mode == "v1":
            detail = (
                "Azure v1 Responses API 调用失败。请确认 base_url 已指向 /openai/v1、"
                "region、deployment name 和模型能力支持 Responses API/function tools。"
                f"deployment={mc.model_name}; status={exc.status_code}; body={exc.body}"
            )
        else:
            api_version = getattr(mc, "api_version", None) or "<empty>"
            responses_url = f"{mc.base_url.rstrip('/')}/openai/responses?api-version={api_version}"
            detail = (
                "Azure legacy Responses API 调用失败。当前会通过 Azure SDK 请求 "
                f"{responses_url}。请确认内部 Azure 代理支持 Responses API/function tools；"
                "如果不支持，只能关闭该 Agent 节点思考或改用明确支持 Responses 的 v1 网关配置。"
                f"deployment={mc.model_name}; status={exc.status_code}; body={exc.body}"
            )
        raise HTTPException(
            status_code=422,
            detail=detail,
        ) from exc
    raise exc


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
    sess = AgentSession(
        user_id=user.id,
        title=f"会话 {seq}",
        models_json=json.dumps(models),
        model_params_json=json.dumps(_role_model_params(body.model_params), ensure_ascii=False),
    )
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
        await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.session_id.in_(sids)))
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
    await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.session_id == sid))
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
    params: dict | None = None


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
    preview_tools = make_preview_tools(get_session_factory(), user.id,
                                       workflow_id=body.workflow_id, node_id=body.node_id)
    try:
        with log_context(user_id=user.id, workflow_id=body.workflow_id,
                         node_id=body.node_id, source="codegen"):
            result = await generate_code(mc, body.instruction, columns, current_code=body.current_code or "",
                                         preview_tools=preview_tools, params=body.params)
    except ModelHTTPError as exc:
        _raise_model_http_error(exc, mc)
    except ValueError as e:   # 模型未产出有效代码 JSON → 可读 422，而非裸 500
        raise HTTPException(status_code=422, detail=str(e))
    return {"code": result["code"], "output_columns": result["output_columns"],
            "columns": columns, "sample_source": source}


class NodeAssistIn(BaseModel):
    workflow_id: int
    node_id: str
    node_type: str
    instruction: str
    model_config_id: int
    current_config: dict | None = None
    params: dict | None = None
    history: list[dict] = []


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
    preview_tools = make_preview_tools(get_session_factory(), user.id,
                                       workflow_id=body.workflow_id, node_id=body.node_id)
    try:
        with log_context(user_id=user.id, workflow_id=body.workflow_id,
                         node_id=body.node_id, source="assistant"):
            r = await codegen_mod.generate_node_config(
                mc, body.node_type, body.instruction, columns, current_config=body.current_config,
                preview_tools=preview_tools, params=body.params, history=body.history)
    except ModelHTTPError as exc:
        _raise_model_http_error(exc, mc)
    return {"reply": r["reply"], "config": r["config"], "sample_source": source}
