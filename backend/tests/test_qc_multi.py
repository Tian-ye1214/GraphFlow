import asyncio
import json

import app.engine.nodes as nodes


def _fake_chat_factory(verdict_by_model):
    async def fake_chat(mc, system, user, params=None, retries=3):
        v = verdict_by_model[mc.id]
        return json.dumps(v), {"prompt_tokens": 1, "completion_tokens": 1}
    return fake_chat


class _MC:
    def __init__(self, id): self.id = id


async def test_k_of_n_pass(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"status": "pass", "reason": "好"}, 2: {"status": "failed", "reason": "太短"},
        3: {"status": "pass", "reason": "好"}}))
    sem = asyncio.Semaphore(4)
    cfg = {"system_prompt": "", "user_prompt": "{{q}}"}
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        cfg, {"q": "hello"}, [_MC(1), _MC(2), _MC(3)], 2, sem)
    assert ok is True                       # 2/3 通过 ≥ K=2
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3}
    assert {p["model_config_id"] for p in per_model} == {1, 2, 3}


async def test_k_of_n_fail_aggregates_reasons(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"status": "failed", "reason": "太短"}, 2: {"status": "factual_error", "reason": "跑题"}}))
    sem = asyncio.Semaphore(4)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"system_prompt": "", "user_prompt": "{{q}}"}, {"q": "x"}, [_MC(1), _MC(2)], 2, sem)
    assert ok is False                      # 0/2 ≥ 2 → 不通过
    assert "太短" in reason and "跑题" in reason
    assert {p["status"] for p in per_model} == {"failed", "factual_error"}
