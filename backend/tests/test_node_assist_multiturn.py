import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from app.agent import codegen
from app.services.workflow_package import REDACTED


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


async def test_generate_node_config_redacts_http_secrets_before_model():
    """http_fetch 的 current_config 含固化密钥（params.api_key / 鉴权头）时，喂给模型的提示词
    必须脱敏——否则密钥值进模型输入并被 ModelCallLog 持久化（可经 model-logs 查到），违反密钥不外泄。"""
    seen = {}

    def fn(messages, info):
        seen["prompt"] = "\n".join(
            str(p.content) for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[TextPart('{"reply":"好","config":null}')])

    cfg = {
        "method": "GET",
        "endpoint": "https://api.example.com/weather",
        "params": {"city": "{{city}}", "api_key": "supersecret123"},
        "headers": {"Authorization": "Bearer tok_xyz789"},
    }
    await codegen.generate_node_config(FunctionModel(fn), "http_fetch", "加个城市参数",
                                       ["city"], current_config=cfg)
    assert "supersecret123" not in seen["prompt"]
    assert "tok_xyz789" not in seen["prompt"]
    assert REDACTED in seen["prompt"]
    # 模板值与非密钥结构保留，助手仍能基于现有配置增量
    assert "{{city}}" in seen["prompt"]
    assert "api_example.com/weather".replace("_", ".") in seen["prompt"]
    # 调用方原 dict 不被脱敏改动（脱敏作用于副本）
    assert cfg["params"]["api_key"] == "supersecret123"


async def test_generate_node_config_nonstring_history_no_crash():
    """前端不可信 history 的非字符串 text(int/float/bool)经真实 pydantic_ai 消息映射路径不得抛
    TypeError(`for part in content`)→端点 500；强转 str 后正常返回。"""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart('{"reply":"好","config":null}')])

    history = [{"role": "user", "text": 123}, {"role": "assistant", "text": 4.5},
               {"role": "user", "text": True}]
    r = await codegen.generate_node_config(FunctionModel(fn), "qc", "再严格点", ["q"], history=history)
    assert r["reply"] == "好" and r["config"] is None
