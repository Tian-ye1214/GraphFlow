from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ModelCallLog, ModelConfig
from app.services import llm, model_log


def _mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen", base_url="http://x/v1",
                       api_key_enc=crypto.encrypt("sk-1"), default_params_json="{}")


def _fake_resp(text="好"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))


def _patch_client(monkeypatch, text="好"):
    async def create(**kw):
        return _fake_resp(text)
    monkeypatch.setattr(llm, "_client",
                        lambda _: SimpleNamespace(chat=SimpleNamespace(
                            completions=SimpleNamespace(create=create))))


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)


async def test_chat_logs_with_context(session_factory, monkeypatch):
    _patch_client(monkeypatch)
    with model_log.log_context(user_id=1, run_id=5, node_id="ls", source="synth"):
        await llm.chat(_mc(), "系统", "用户")
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog))).scalars().first()
    assert row is not None and row.source == "synth" and row.node_id == "ls"
    assert "用户" in row.request_json and "好" in row.response_json
    assert "sk-1" not in row.request_json   # 不泄露密钥


async def test_chat_no_context_no_log(session_factory, monkeypatch):
    _patch_client(monkeypatch)
    await llm.chat(_mc(), "", "u")          # 无上下文
    async with session_factory() as s:
        assert (await s.execute(select(ModelCallLog))).scalars().first() is None


async def test_logging_model_wraps_agent_run(session_factory):
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from app.agent.logging_model import LoggingModel

    agent = Agent(LoggingModel(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("你好")]))))
    with model_log.log_context(user_id=1, session_id=9, source="redlotus"):
        result = await agent.run("hi")
    assert "你好" in str(result.output)
    async with session_factory() as s:
        rows = (await s.execute(
            select(ModelCallLog).where(ModelCallLog.source == "redlotus"))).scalars().all()
    assert len(rows) >= 1 and "hi" in rows[0].request_json and "你好" in rows[0].response_json

