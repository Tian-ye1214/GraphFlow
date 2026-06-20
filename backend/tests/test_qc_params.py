import asyncio

from app.engine import nodes
from app.models import ModelConfig


async def test_qc_params_user_overrides_temperature(monkeypatch):
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"status": "pass", "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    config = {"system_prompt": "判定", "user_prompt": "{{q}}",
              "params": {"temperature": 0.7, "top_p": 0.9}}
    await nodes.run_qc_judge_row(config, {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["temperature"] == 0.7
    assert seen["params"]["top_p"] == 0.9
    assert seen["params"]["json_mode"] is True


async def test_qc_params_default_temperature_zero(monkeypatch):
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"status": "pass", "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    await nodes.run_qc_judge_row({"system_prompt": "判定", "user_prompt": "{{q}}"},
                                 {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["temperature"] == 0  # 未设时默认确定性
    assert seen["params"]["json_mode"] is True


async def test_qc_params_thinking_default_high(monkeypatch):
    """判定默认：思考开启 + 力度 high + max_tokens 65536（未设节点 params 时）。"""
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"status": "pass", "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    await nodes.run_qc_judge_row({"system_prompt": "判定", "user_prompt": "{{q}}"},
                                 {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["thinking_enabled"] is True
    assert seen["params"]["reasoning_effort"] == "high"
    assert seen["params"]["max_tokens"] == 65536


async def test_qc_params_thinking_user_overridable(monkeypatch):
    """节点默认非写死：用户在 params 里关思考/调档/改 max_tokens 均生效（覆盖默认）。"""
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"status": "pass", "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    config = {"system_prompt": "判定", "user_prompt": "{{q}}",
              "params": {"thinking_enabled": False, "reasoning_effort": "low", "max_tokens": 100}}
    await nodes.run_qc_judge_row(config, {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["thinking_enabled"] is False
    assert seen["params"]["reasoning_effort"] == "low"
    assert seen["params"]["max_tokens"] == 100
