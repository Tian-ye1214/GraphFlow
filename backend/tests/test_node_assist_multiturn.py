import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from app.agent import codegen


async def test_generate_node_config_returns_reply_and_config():
    out = json.dumps({"reply": "已按需求生成翻译配置", "config": {
        "system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}}, ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```json\n{out}\n```")]))
    r = await codegen.generate_node_config(model, "llm_synth", "把 q 翻译成英文", ["q"])
    assert r["reply"] == "已按需求生成翻译配置"
    assert r["config"]["output_column"] == "q_en"


async def test_generate_node_config_passes_history():
    seen = {}

    def fn(messages, info):
        seen["n_user"] = sum(1 for m in messages if isinstance(m, ModelRequest)
                             for p in m.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[TextPart('{"reply":"好","config":null}')])

    history = [{"role": "user", "text": "第一轮"}, {"role": "assistant", "text": "回应"}]
    r = await codegen.generate_node_config(FunctionModel(fn), "qc", "再严格点", ["q"], history=history)
    assert r["config"] is None and r["reply"] == "好"
    assert seen["n_user"] == 2   # 历史里 1 条 user + 本轮 instruction 1 条
