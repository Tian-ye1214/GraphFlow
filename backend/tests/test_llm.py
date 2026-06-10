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


async def test_model_test_endpoint(auth_client, monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response("pong"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    payload = {"name": "m", "model_name": "qwen-max", "base_url": "http://x/v1",
               "api_key": "sk-1", "default_params": {}}
    mid = (await auth_client.post("/api/models", json=payload)).json()["id"]
    r = (await auth_client.post(f"/api/models/{mid}/test")).json()
    assert r == {"ok": True, "reply": "pong"}
