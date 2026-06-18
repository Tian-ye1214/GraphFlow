"""Agent 路径模型日志网关：包一层 WrapperModel，request/request_stream 返回后落库。
经 factory.create_model 统一套上，覆盖所有 agent（coordinator/manager/worker/codegen/节点助手）。"""
from contextlib import asynccontextmanager

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.wrapper import WrapperModel

from app.services.model_log import current_ctx, log_model_call


def _texts(parts) -> str:
    return "\n".join(p.content for p in parts
                     if isinstance(getattr(p, "content", None), str))


def _dump(messages) -> list:
    try:
        return ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    except Exception:
        return [{"kind": getattr(m, "kind", "?"), "text": _texts(getattr(m, "parts", []))}
                for m in messages]


class LoggingModel(WrapperModel):
    async def request(self, messages, model_settings, model_request_parameters):
        resp = await super().request(messages, model_settings, model_request_parameters)
        await self._log(messages, resp)
        return resp

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with super().request_stream(messages, model_settings,
                                          model_request_parameters, run_context) as rs:
            yield rs
        await self._log(messages, rs.get())

    async def _log(self, messages, resp):
        if current_ctx() is None:
            return
        usage = getattr(resp, "usage", None)
        await log_model_call(
            messages=_dump(messages), response_text=_texts(getattr(resp, "parts", [])),
            ok=True, model_name=self.model_name, provider=(self.system or ""),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0)
