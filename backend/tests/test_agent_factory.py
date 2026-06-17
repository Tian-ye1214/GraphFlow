from pydantic_ai.models.test import TestModel

from app import crypto
from app.agent import factory
from app.models import ModelConfig


def _mc(**over):
    base = dict(user_id=1, name="m1", model_name="qwen-max", base_url="http://llm.local/v1",
                api_key_enc=crypto.encrypt("sk-test"),
                default_params_json='{"temperature": 0.3, "max_tokens": 100, "json_mode": true}')
    base.update(over)
    return ModelConfig(**base)


def test_create_model_decrypts_key(monkeypatch):
    captured = {}
    real = factory.OpenAIProvider

    def spy(base_url, api_key):
        captured.update(base_url=base_url, api_key=api_key)
        return real(base_url=base_url, api_key=api_key)

    monkeypatch.setattr(factory, "OpenAIProvider", spy)
    model = factory.create_model(_mc())
    assert captured == {"base_url": "http://llm.local/v1", "api_key": "sk-test"}
    assert model.model_name == "qwen-max"
    assert model.settings["temperature"] == 0.3
    assert model.settings["max_tokens"] == 100
    assert "json_mode" not in model.settings  # 非 ModelSettings 键被忽略


def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    # 思考默认开启 → settings 带 extra_body，不再为 None
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_create_model_thinking_disabled():
    model = factory.create_model(_mc(default_params_json='{"thinking_enabled": false}'))
    assert "extra_body" not in (model.settings or {})


async def test_create_agent_runs_tools():
    async def ping(text: str) -> str:
        """回声工具。
        Parameters:
            text: 文本
        """
        return f"pong:{text}"

    agent = factory.create_agent(TestModel(), [ping], "你是测试")
    result = await agent.run("hi")
    assert "pong:" in str(result.output)
