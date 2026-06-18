import asyncio

from app.engine import nodes
from app.models import ModelConfig


async def test_qc_params_user_overrides_temperature(monkeypatch):
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"pass": true, "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

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
        return '{"pass": true, "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    await nodes.run_qc_judge_row({"system_prompt": "判定", "user_prompt": "{{q}}"},
                                 {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["temperature"] == 0  # 未设时默认确定性
    assert seen["params"]["json_mode"] is True
