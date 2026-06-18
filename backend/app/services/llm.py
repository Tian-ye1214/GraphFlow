import asyncio
import json

from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.llm_clients import azure_api_mode, make_chat_client, provider_name
from app.models import ModelConfig
from app.services.model_log import log_model_call
from app.thinking import chat_thinking_kwargs, thinking_enabled

BACKOFF_BASE = 1

_client_cache: dict[tuple[str, str, str, str, str], AsyncOpenAI | AsyncAzureOpenAI] = {}


class LLMError(Exception):
    pass


def _client(mc: ModelConfig) -> AsyncOpenAI | AsyncAzureOpenAI:
    provider = provider_name(mc)
    mode = azure_api_mode(mc) if provider == "azure" else "openai"
    api_version = getattr(mc, "api_version", None) or ""
    cache_key = (provider, mode, mc.base_url, api_version, mc.api_key_enc or "")
    if cache_key not in _client_cache:
        _client_cache[cache_key] = make_chat_client(mc)
    return _client_cache[cache_key]


def _request_kwargs(mc: ModelConfig, merged: dict, call_params: dict) -> dict:
    provider = provider_name(mc)
    kwargs: dict = {}
    reasoning_on = thinking_enabled(call_params)

    if provider == "azure" and reasoning_on:
        if merged.get("max_tokens") is not None:
            kwargs["max_completion_tokens"] = merged["max_tokens"]
    else:
        for key in ("temperature", "top_p", "max_tokens"):
            if merged.get(key) is not None:
                kwargs[key] = merged[key]

    if merged.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}

    kwargs.update(chat_thinking_kwargs(call_params, provider=provider))
    return kwargs


async def chat(mc: ModelConfig, system_prompt: str, user_prompt: str,
               params: dict | None = None, retries: int = 3) -> tuple[str, dict]:
    model_defaults = json.loads(mc.default_params_json)
    call_params = dict(params or {})
    merged = {**model_defaults, **call_params}
    kwargs = _request_kwargs(mc, merged, call_params)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    client = _client(mc)
    provider = provider_name(mc)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=mc.model_name,
                messages=messages,
                timeout=merged.get("timeout", 120),
                **kwargs,
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                raise LLMError("模型返回空内容")
            usage = {"prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                     "completion_tokens": resp.usage.completion_tokens if resp.usage else 0}
            await log_model_call(messages=messages, response_text=content, ok=True,
                                 model_name=mc.model_name, provider=provider,
                                 prompt_tokens=usage["prompt_tokens"],
                                 completion_tokens=usage["completion_tokens"], model_config_id=mc.id)
            return content, usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    await log_model_call(messages=messages, response_text=f"[失败] {last_err}", ok=False,
                         model_name=mc.model_name, provider=provider, model_config_id=mc.id)
    raise LLMError(str(last_err))
