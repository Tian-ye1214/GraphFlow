import asyncio
import json

import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app import crypto
from app.models import ModelConfig
from app.thinking import thinking_extra_body

BACKOFF_BASE = 1  # 秒；重试等待 BACKOFF_BASE * 2**attempt，测试中置 0

_client_cache: dict[tuple[str, str, str, str], AsyncOpenAI | AsyncAzureOpenAI] = {}


class LLMError(Exception):
    pass


def _provider(mc: ModelConfig) -> str:
    return (getattr(mc, "provider", None) or "openai").lower()


def _client(mc: ModelConfig) -> AsyncOpenAI | AsyncAzureOpenAI:
    """按 provider/base_url/api_version/api_key_enc 缓存客户端：避免每行调用重建 SSL 上下文（约 45ms 阻塞）。
    重试由 chat() 的外层循环负责，故关闭 SDK 内置重试。"""
    provider = _provider(mc)
    api_version = getattr(mc, "api_version", None) or ""
    cache_key = (provider, mc.base_url, api_version, mc.api_key_enc or "")
    if cache_key not in _client_cache:
        api_key = crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none"
        if provider == "azure":
            _client_cache[cache_key] = AsyncAzureOpenAI(
                azure_endpoint=mc.base_url,
                api_key=api_key,
                api_version=api_version,
                max_retries=3,
                http_client=httpx.AsyncClient(http2=True),
            )
        else:
            _client_cache[cache_key] = AsyncOpenAI(base_url=mc.base_url, api_key=api_key,
                                                   max_retries=3, http_client=httpx.AsyncClient(http2=True))
    return _client_cache[cache_key]


async def chat(mc: ModelConfig, system_prompt: str, user_prompt: str,
               params: dict | None = None, retries: int = 3) -> tuple[str, dict]:
    """单次对话调用。返回 (文本, usage)。重试耗尽抛 LLMError。"""
    merged = {**json.loads(mc.default_params_json), **(params or {})}
    kwargs: dict = {}
    for key in ("temperature", "top_p", "max_tokens"):
        if merged.get(key) is not None:
            kwargs[key] = merged[key]
    if merged.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}
    eb = thinking_extra_body(merged)
    if eb is not None:
        kwargs["extra_body"] = eb
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    client = _client(mc)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=mc.model_name, messages=messages,
                timeout=merged.get("timeout", 120), **kwargs)
            content = resp.choices[0].message.content or ""
            if not content.strip():  # 空/全空白补全视为可重试失败，避免空结果被当成功落库污染产物
                raise LLMError("模型返回空内容")
            usage = {"prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                     "completion_tokens": resp.usage.completion_tokens if resp.usage else 0}
            return content, usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    raise LLMError(str(last_err))
