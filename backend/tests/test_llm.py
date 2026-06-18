from types import SimpleNamespace

import pytest

from app.models import ModelConfig
from app.services import llm


def fake_response(text="好的"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeClient:
    """behavior(call_no, kwargs) -> response 或抛异常"""

    def __init__(self, behavior):
        self.calls = 0
        outer = self

        async def create(**kwargs):
            outer.calls += 1
            outer.last_kwargs = kwargs
            return behavior(outer.calls, kwargs)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen-max",
                       base_url="http://x/v1", api_key_enc=crypto.encrypt("sk-1"),
                       default_params_json='{"temperature": 0.5}')


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)


async def test_chat_success(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response("你好"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    text, usage = await llm.chat(mc(), "系统", "用户")
    assert text == "你好"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert fake.last_kwargs["model"] == "qwen-max"
    assert fake.last_kwargs["temperature"] == 0.5  # default_params 生效
    assert fake.last_kwargs["messages"][0] == {"role": "system", "content": "系统"}


async def test_params_override_and_json_mode(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"temperature": 0.9, "json_mode": True, "max_tokens": 100})
    assert fake.last_kwargs["temperature"] == 0.9
    assert fake.last_kwargs["max_tokens"] == 100
    assert fake.last_kwargs["response_format"] == {"type": "json_object"}
    assert fake.last_kwargs["messages"][0]["role"] == "user"  # 空 system 不发送


async def test_retry_then_success(monkeypatch):
    def behavior(n, kw):
        if n == 1:
            raise RuntimeError("boom")
        return fake_response()

    fake = FakeClient(behavior)
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    text, _ = await llm.chat(mc(), "", "u", retries=3)
    assert fake.calls == 2


async def test_retries_exhausted(monkeypatch):
    def behavior(n, kw):
        raise RuntimeError("always")

    fake = FakeClient(behavior)
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    with pytest.raises(llm.LLMError, match="always"):
        await llm.chat(mc(), "", "u", retries=2)
    assert fake.calls == 2


async def test_empty_completion_retries_then_raises(monkeypatch):
    """空/全空白补全视为可重试失败：重试到耗尽后抛 LLMError，而非把空当成功返回。"""
    fake = FakeClient(lambda n, kw: fake_response("   "))  # 每次都全空白
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    with pytest.raises(llm.LLMError, match="空内容"):
        await llm.chat(mc(), "", "u", retries=2)
    assert fake.calls == 2  # 两次都因空被重试


async def test_empty_then_nonempty_succeeds(monkeypatch):
    """首次空、二次非空：重试拿到实质内容即成功返回。"""
    fake = FakeClient(lambda n, kw: fake_response("" if n == 1 else "实质内容"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    text, _ = await llm.chat(mc(), "", "u", retries=3)
    assert text == "实质内容"
    assert fake.calls == 2


async def test_model_test_endpoint(auth_client, monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response("pong"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    payload = {"name": "m", "model_name": "qwen-max", "base_url": "http://x/v1",
               "api_key": "sk-1", "default_params": {}}
    mid = (await auth_client.post("/api/models", json=payload)).json()["id"]
    r = (await auth_client.post(f"/api/models/{mid}/test")).json()
    assert r == {"ok": True, "reply": "pong"}


async def test_thinking_default_extra_body(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u")
    assert fake.last_kwargs["reasoning_effort"] == "high"
    assert fake.last_kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


async def test_thinking_disabled_no_extra_body(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"thinking_enabled": False})
    assert "extra_body" not in fake.last_kwargs


async def test_thinking_custom_effort(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"reasoning_effort": "medium"})
    assert fake.last_kwargs["reasoning_effort"] == "medium"
