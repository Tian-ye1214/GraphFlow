import asyncio

from app.engine import nodes
from app.services import llm


def test_qc_split_pass_fail():
    rows = [{"a": "good"}, {"a": ""}, {"a": "x"}]
    cfg = {"condition": {"column": "a", "mode": "not_empty", "value": None}}
    passed, failed = nodes.qc_split(rows, cfg)
    assert passed == [{"a": "good"}, {"a": "x"}]
    assert failed == [{"a": "", "_qc_reason": "列「a」未通过质检（not_empty）"}]


def test_qc_split_reason_field():
    rows = [{"a": "1", "judge": "fail", "why": "太短"},
            {"a": "2", "judge": "pass", "why": ""}]
    cfg = {"condition": {"column": "judge", "mode": "equals", "value": "pass"},
           "reason_field": "why"}
    passed, failed = nodes.qc_split(rows, cfg)
    assert [r["a"] for r in passed] == ["2"]
    assert failed[0]["_qc_reason"] == "太短"


async def test_reason_injected_into_prompt_and_stripped(monkeypatch):
    captured = {}

    async def fake(mc, system, user, params=None, retries=3):
        captured["user"] = user
        return "新结果", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    cfg = {"user_prompt": "Q:{{q}}", "output_column": "a"}
    row = {"q": "问", "a": "旧", "_qc_reason": "字段缺失"}
    out, usage = await nodes.run_llm_synth_row(cfg, row, None, asyncio.Semaphore(1))
    assert "Q:问" in captured["user"] and "字段缺失" in captured["user"]
    assert out == [{"q": "问", "a": "新结果"}]  # _qc_reason 不进提示模板变量也不留在输出
