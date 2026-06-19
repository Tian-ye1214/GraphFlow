import asyncio
import json

import pytest

from app.engine import nodes
from app.models import ModelConfig
from app.services import llm


def mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen", base_url="http://x/v1",
                       api_key_enc=crypto.encrypt("k"), default_params_json="{}")


def patch_chat(monkeypatch, fn):
    async def fake_chat(mc_, system, user, params=None, retries=3):
        return fn(system, user)
    monkeypatch.setattr(llm, "chat", fake_chat)


def test_render_template():
    assert nodes.render_template("改写：{{q}}，难度{{ level }}", {"q": "你好", "level": 5}) == "改写：你好，难度5"
    assert nodes.render_template("缺列：{{nope}}!", {}) == "缺列：!"


async def test_column_mode(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: (f"回答[{u}]", {"prompt_tokens": 3, "completion_tokens": 7}))
    config = {"system_prompt": "sys", "user_prompt": "Q: {{q}}", "output_mode": "column",
              "output_column": "answer"}
    out, usage = await nodes.run_llm_synth_row(config, {"q": "你好"}, mc(), asyncio.Semaphore(8))
    assert out == [{"q": "你好", "answer": "回答[Q: 你好]"}]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 7}


async def test_json_mode_merges_columns(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: (json.dumps({"a": 1, "b": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}))
    config = {"user_prompt": "u", "output_mode": "json"}
    out, _ = await nodes.run_llm_synth_row(config, {"q": "原"}, mc(), asyncio.Semaphore(8))
    assert out == [{"q": "原", "a": 1, "b": "x"}]


async def test_json_mode_non_object_raises(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: ("[1,2]", {"prompt_tokens": 1, "completion_tokens": 1}))
    with pytest.raises(ValueError, match="JSON 对象"):
        await nodes.run_llm_synth_row({"user_prompt": "u", "output_mode": "json"}, {}, mc(), asyncio.Semaphore(8))


async def test_fanout(monkeypatch):
    counter = {"n": 0}

    def fn(s, u):
        counter["n"] += 1
        return f"变体{counter['n']}", {"prompt_tokens": 1, "completion_tokens": 2}

    patch_chat(monkeypatch, fn)
    config = {"user_prompt": "u", "fanout_n": 3, "output_column": "v"}
    out, usage = await nodes.run_llm_synth_row(config, {"q": 1}, mc(), asyncio.Semaphore(8))
    assert len(out) == 3
    assert {r["v"] for r in out} == {"变体1", "变体2", "变体3"}
    assert usage == {"prompt_tokens": 3, "completion_tokens": 6}


async def test_fanout_zero_or_negative_rejected(monkeypatch):
    """fanout_n<=0：否则 range(0) 不发请求、该行静默产出 0 行却被记 done，输入行凭空丢失而 run 仍 completed。"""
    patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 1, "completion_tokens": 1}))
    for bad in (0, -1):
        with pytest.raises(ValueError):
            await nodes.run_llm_synth_row({"user_prompt": "u", "fanout_n": bad}, {"q": "原"},
                                          mc(), asyncio.Semaphore(8))


async def test_empty_output_column_falls_back_to_output(monkeypatch):
    """output_column 设为空串 → 兜底 'output'（与 columns.py 血缘一致），不落进无名 '' 列。"""
    patch_chat(monkeypatch, lambda s, u: ("答案", {"prompt_tokens": 1, "completion_tokens": 1}))
    out, _ = await nodes.run_llm_synth_row({"user_prompt": "u", "output_column": ""}, {"q": "原"},
                                           mc(), asyncio.Semaphore(8))
    assert out == [{"q": "原", "output": "答案"}]


async def test_semaphore_limits_concurrency(monkeypatch):
    state = {"now": 0, "peak": 0}

    async def fake_chat(mc_, system, user, params=None, retries=3):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", fake_chat)
    config = {"user_prompt": "u", "fanout_n": 10, "output_column": "v"}
    await nodes.run_llm_synth_row(config, {}, mc(), asyncio.Semaphore(2))
    assert state["peak"] <= 2


async def test_partial_fanout_failure_releases_semaphore(monkeypatch):
    """扇出部分失败后，兄弟任务必须被取消并立刻释放信号量。"""
    calls = {"n": 0}

    async def fake_chat(mc_, system, user, params=None, retries=3):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.05)
            raise RuntimeError("llm error")
        await asyncio.sleep(0.3)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", fake_chat)
    sem = asyncio.Semaphore(2)
    config = {"user_prompt": "u", "fanout_n": 3, "output_column": "v"}
    with pytest.raises(RuntimeError):
        await nodes.run_llm_synth_row(config, {}, mc(), sem)
    await asyncio.sleep(0.1)  # 留出取消传播时间；远小于兄弟任务 0.3s 的自然完成时间
    assert sem._value == 2, "兄弟任务在行失败后仍占用信号量"
