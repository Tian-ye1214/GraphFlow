from pydantic_ai.models.test import TestModel

from app import crypto, llm_clients
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
    real = factory.make_agent_provider

    def spy(mc):
        captured.update(base_url=mc.base_url, api_key=crypto.decrypt(mc.api_key_enc))
        return real(mc)

    monkeypatch.setattr(factory, "make_agent_provider", spy)
    model = factory.create_model(_mc())
    assert captured == {"base_url": "http://llm.local/v1", "api_key": "sk-test"}
    assert model.model_name == "qwen-max"
    assert model.settings["temperature"] == 0.3
    assert model.settings["max_tokens"] == 65536  # force_max_thinking 写死 65536，覆盖模型配置的 100
    assert "json_mode" not in model.settings  # 非 ModelSettings 键被忽略


def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    # 写死 max：agent 路径 extra_body 含 thinking + reasoning_effort=max
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "max"}


def test_create_model_thinking_forced_max_even_if_disabled():
    # 硬编码：即便请求方传 thinking_enabled:false / 低力度，仍强制 思考开 + 力度 max
    model = factory.create_model(_mc(), params={"thinking_enabled": False, "reasoning_effort": "low"})
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "max"}


def test_agent_http_client_cached_per_config():
    """H1 回归：agent 路径 httpx 客户端按 model 配置缓存复用，避免每次 create_model 都新建
    一个永不关闭的 AsyncClient（长跑累积致 socket/FD 泄漏）。对照 services/llm.py 的 _client 缓存。"""
    llm_clients._agent_client_cache.clear()
    mc1 = _mc()
    llm_clients.make_agent_provider(mc1)
    llm_clients.make_agent_provider(mc1)
    assert len(llm_clients._agent_client_cache) == 1            # 同配置只建一个 client
    c1 = llm_clients._agent_http_client(mc1)
    assert llm_clients._agent_http_client(mc1) is c1            # 复用同一对象
    mc2 = _mc(base_url="http://other.local/v1")
    llm_clients.make_agent_provider(mc2)
    assert len(llm_clients._agent_client_cache) == 2            # 不同配置各自一个
    assert llm_clients._agent_http_client(mc2) is not c1


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
