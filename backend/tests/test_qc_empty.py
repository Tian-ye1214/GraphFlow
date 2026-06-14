import asyncio
from app.engine import nodes


async def test_empty_sample_fails_without_judge(monkeypatch):
    called = False
    async def boom(*a, **k):
        nonlocal called; called = True
        raise AssertionError("judge 不应被调用")
    monkeypatch.setattr(nodes.llm, "chat", boom)
    sem = asyncio.Semaphore(4)
    for row in ({}, {"q_en": "", "category_en": "   "}):
        ok, reason, usage, per_model = await nodes.run_qc_judge_row(
            {"system_prompt": "判断", "user_prompt": "{{q_en}}"}, row, [object()], 1, sem)
        assert ok is False and "空" in reason and per_model == []
    assert called is False


async def test_judge_uses_temperature_zero_and_anchor(monkeypatch):
    seen = {}
    async def fake_chat(mc, system, user, params=None, retries=3):
        seen.update(params or {}); seen["system"] = system
        return '{"pass": true, "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}
    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    class MC: id = 7
    ok, *_ = await nodes.run_qc_judge_row(
        {"system_prompt": "判断", "user_prompt": "{{q}}"}, {"q": "非空内容"}, [MC()], 1,
        asyncio.Semaphore(4))
    assert ok is True
    assert seen.get("temperature") == 0
    assert "pass:false" in seen["system"]
