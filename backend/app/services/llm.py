import asyncio
import json

from openai import AsyncOpenAI

from app import crypto
from app.models import ModelConfig

BACKOFF_BASE = 1  # 秒；重试等待 BACKOFF_BASE * 2**attempt，测试中置 0


class LLMError(Exception):
    pass


def _client(mc: ModelConfig) -> AsyncOpenAI:
    api_key = crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none"
    return AsyncOpenAI(base_url=mc.base_url, api_key=api_key)


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
            usage = {"prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                     "completion_tokens": resp.usage.completion_tokens if resp.usage else 0}
            return resp.choices[0].message.content or "", usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    raise LLMError(str(last_err))
