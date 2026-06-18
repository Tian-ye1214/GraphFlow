"""模型/Agent 调用日志：唯一落库切口 + 上下文 contextvar。两条网关共用。
铁律：只记 messages 与响应文本，绝不记 api_key/Authorization 头。"""
import contextlib
import contextvars
import json

from loguru import logger

from app.db import get_session_factory
from app.models import ModelCallLog

NODE_LIMIT = 20                       # 节点类(synth/qc) 每 (run,node) 成功记录上限
_NODE_SOURCES = ("synth", "qc")
_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("model_log_ctx", default=None)
_success_counts: dict[tuple, int] = {}


@contextlib.contextmanager
def log_context(**ctx):
    token = _ctx.set({**(_ctx.get() or {}), **ctx})
    try:
        yield
    finally:
        _ctx.reset(token)


def current_ctx() -> dict | None:
    return _ctx.get()


def _should_log(ctx: dict, ok: bool) -> bool:
    if ctx.get("source") not in _NODE_SOURCES:
        return True                   # Agent 类全量
    if not ok:
        return True                   # 失败行全留
    key = (ctx.get("run_id"), ctx.get("node_id"))
    if _success_counts.get(key, 0) >= NODE_LIMIT:
        return False
    _success_counts[key] = _success_counts.get(key, 0) + 1
    return True


async def log_model_call(*, messages, response_text, ok, model_name, provider,
                         prompt_tokens=0, completion_tokens=0, model_config_id=None):
    ctx = _ctx.get()
    if ctx is None:                   # 无上下文（连通测试/单测）不记
        return
    try:
        if not _should_log(ctx, ok):
            return
        logger.bind(source=ctx.get("source"), run_id=ctx.get("run_id"),
                    node_id=ctx.get("node_id"), ok=ok).info("model_call")
        async with get_session_factory()() as s:
            s.add(ModelCallLog(
                user_id=ctx.get("user_id") or 0, run_id=ctx.get("run_id"),
                workflow_id=ctx.get("workflow_id"), session_id=ctx.get("session_id"),
                node_id=ctx.get("node_id") or "", source=ctx.get("source") or "",
                model_config_id=model_config_id, model_name=model_name, provider=provider,
                request_json=json.dumps(messages, ensure_ascii=False),
                response_json=response_text or "",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))
            await s.commit()
    except Exception as e:            # 记日志失败绝不影响主调用
        logger.warning(f"model_log 落库失败(忽略): {e}")
