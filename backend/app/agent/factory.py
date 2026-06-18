import json

from pydantic_ai import Agent, FunctionToolset, ModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from app.agent.logging_model import LoggingModel
from app.agent.tools import wrap_tools
from app.llm_clients import azure_api_mode, make_agent_provider, provider_name
from app.models import ModelConfig
from app.thinking import agent_chat_settings, agent_responses_settings, force_xhigh, thinking_enabled

SETTINGS_KEYS = ("temperature", "top_p", "max_tokens", "timeout")


class AzureLegacyChatModel(OpenAIChatModel):
    """Accept AIDP/Azure legacy proxy ChatCompletion responses with null choice indexes."""

    def _validate_completion(self, response):
        for idx, choice in enumerate(response.choices or []):
            if getattr(choice, "index", None) is None:
                choice.index = idx
        return super()._validate_completion(response)


def create_model(mc: ModelConfig, params: dict | None = None) -> OpenAIChatModel | OpenAIResponsesModel:
    default_params = json.loads(mc.default_params_json)
    call_params = force_xhigh(params)        # 批20：RedLotus+助手一律 xhigh，忽略传入思考参数
    merged = {**default_params, **call_params}
    kw = {k: merged[k] for k in SETTINGS_KEYS if merged.get(k) is not None}
    provider = provider_name(mc)
    azure_mode = azure_api_mode(mc) if provider == "azure" else ""
    use_responses = provider == "azure" and azure_mode == "v1" and thinking_enabled(call_params)
    if use_responses:
        kw.update(agent_responses_settings(call_params, provider=provider))
    else:
        kw.update(agent_chat_settings(call_params, provider=provider))
    if use_responses:
        model_cls = OpenAIResponsesModel
    elif provider == "azure" and azure_mode == "legacy":
        model_cls = AzureLegacyChatModel
    else:
        model_cls = OpenAIChatModel
    model = model_cls(
        mc.model_name,
        provider=make_agent_provider(mc, responses=use_responses),
        settings=ModelSettings(**kw) if kw else None,
    )
    return LoggingModel(model)


def create_agent(model, tools: list, instructions: str, params: dict | None = None) -> Agent:
    if isinstance(model, ModelConfig):
        model = create_model(model, params=params)
    return Agent(model, toolsets=[FunctionToolset(wrap_tools(tools), id="default")],
                 instructions=instructions)
