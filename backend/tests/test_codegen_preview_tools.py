import json

import pytest

from app.agent import codegen


class FakeResult:
    output = "{}"


class FakeAgent:
    async def run(self, prompt):
        return FakeResult()


@pytest.mark.asyncio
async def test_generate_code_uses_preview_tool_and_prompt_mentions_data_preview(monkeypatch):
    captured = {}

    def fake_create_agent(model, tools, instructions):
        captured["tools"] = tools
        captured["instructions"] = instructions
        return FakeAgent()

    monkeypatch.setattr(codegen, "create_agent", fake_create_agent)

    await codegen.generate_code("model", "翻译 q", ["q"], preview_tools=[lambda: None])

    assert len(captured["tools"]) == 1
    assert "可调用数据预览工具" in captured["instructions"] or "可调用数据预览工具" in codegen._user_prompt("x", ["q"])


@pytest.mark.asyncio
async def test_generate_node_config_uses_preview_tool(monkeypatch):
    captured = {}

    def fake_create_agent(model, tools, instructions):
        captured["tools"] = tools
        return FakeAgent()

    monkeypatch.setattr(codegen, "create_agent", fake_create_agent)
    monkeypatch.setitem(codegen.NODE_ASSIST_INSTRUCTIONS, "llm_synth", "sys")

    await codegen.generate_node_config(
        "model",
        "llm_synth",
        "生成",
        ["q"],
        preview_tools=[lambda: None],
    )

    assert len(captured["tools"]) == 1
