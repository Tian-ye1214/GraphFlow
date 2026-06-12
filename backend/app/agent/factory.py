"""从 GraphFlow ModelConfig 构造 pydantic-ai Agent（OpenAI 兼容直连，api_key 现解密现用）。"""
import json

from pydantic_ai import Agent, FunctionToolset, ModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app import crypto
from app.agent.tools import wrap_tools
from app.models import ModelConfig

SETTINGS_KEYS = ("temperature", "top_p", "max_tokens", "timeout")


def create_model(mc: ModelConfig) -> OpenAIChatModel:
    params = json.loads(mc.default_params_json)
    kw = {k: params[k] for k in SETTINGS_KEYS if params.get(k) is not None}
    provider = OpenAIProvider(
        base_url=mc.base_url,
        api_key=crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none")
    return OpenAIChatModel(mc.model_name, provider=provider,
                           settings=ModelSettings(**kw) if kw else None)


def create_agent(model, tools: list, instructions: str) -> Agent:
    """model 可传 ModelConfig（按配置构造）或现成 Model 实例（测试用 TestModel/FunctionModel）。"""
    if isinstance(model, ModelConfig):
        model = create_model(model)
    return Agent(model, toolsets=[FunctionToolset(wrap_tools(tools), id="default")],
                 instructions=instructions)
