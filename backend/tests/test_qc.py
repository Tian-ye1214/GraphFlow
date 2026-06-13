import asyncio
import json

import pytest

from app.engine import nodes
from app.services import llm


async def test_qc_judge_parses_verdict(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        assert params and params.get("json_mode") is True  # 判定强制 json 模式
        assert "译文:hello" in user  # 用 base 渲染（剥离 _qc_reason）
        return json.dumps({"pass": False, "reason": "不是中文"}), {"prompt_tokens": 2, "completion_tokens": 3}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage = await nodes.run_qc_judge_row(
        {"user_prompt": "译文:{{a}}"}, {"a": "hello", "_qc_reason": "旧"}, None, asyncio.Semaphore(1))
    assert ok is False and reason == "不是中文"
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


async def test_qc_judge_pass(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"pass": True}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _ = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, None, asyncio.Semaphore(1))
    assert ok is True and reason == "未通过质检"  # reason 缺省给通用文案


async def test_qc_judge_missing_pass_raises(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"reason": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    with pytest.raises(ValueError):
        await nodes.run_qc_judge_row({"user_prompt": "p"}, {"a": "x"}, None, asyncio.Semaphore(1))
