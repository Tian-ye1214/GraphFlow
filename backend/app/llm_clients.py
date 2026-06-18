import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.openai import OpenAIProvider

from app import crypto
from app.models import ModelConfig


def provider_name(mc: ModelConfig) -> str:
    return (getattr(mc, "provider", None) or "openai").lower()


def azure_api_mode(mc: ModelConfig) -> str:
    return (getattr(mc, "azure_api_mode", None) or "legacy").lower()


def decrypt_api_key(mc: ModelConfig) -> str:
    return crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none"


def azure_v1_base_url(mc: ModelConfig) -> str:
    base = mc.base_url.rstrip("/")
    if base.endswith("/openai/v1"):
        return base
    if base.endswith("/v1"):
        return base
    if base.endswith("/openai"):
        return f"{base}/v1"
    return f"{base}/openai/v1"


def make_chat_client(mc: ModelConfig) -> AsyncOpenAI | AsyncAzureOpenAI:
    api_key = decrypt_api_key(mc)
    if provider_name(mc) != "azure":
        return AsyncOpenAI(
            base_url=mc.base_url,
            api_key=api_key,
            max_retries=3,
            http_client=httpx.AsyncClient(http2=True),
        )
    if azure_api_mode(mc) == "v1":
        return AsyncOpenAI(
            base_url=azure_v1_base_url(mc),
            api_key=api_key,
            max_retries=3,
            http_client=httpx.AsyncClient(http2=True),
        )
    return AsyncAzureOpenAI(
        azure_endpoint=mc.base_url,
        api_key=api_key,
        api_version=getattr(mc, "api_version", None) or "",
        max_retries=3,
        http_client=httpx.AsyncClient(http2=True),
    )


def make_agent_provider(mc: ModelConfig, *, responses: bool = False) -> AzureProvider | OpenAIProvider:
    api_key = decrypt_api_key(mc)
    if provider_name(mc) != "azure":
        return OpenAIProvider(base_url=mc.base_url, api_key=api_key, http_client=httpx.AsyncClient(http2=True))
    if azure_api_mode(mc) == "v1":
        return AzureProvider(
            azure_endpoint=azure_v1_base_url(mc),
            api_key=api_key,
            http_client=httpx.AsyncClient(http2=True),
        )
    return AzureProvider(
        azure_endpoint=mc.base_url,
        api_key=api_key,
        api_version=getattr(mc, "api_version", None) or "",
        http_client=httpx.AsyncClient(http2=True),
    )
