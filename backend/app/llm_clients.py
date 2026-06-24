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
    # max_retries=0：重试统一交给 services/llm.py 外层循环（带退避+日志+空内容重试）。
    # 若此处也开 SDK 重试，会与外层相乘（3×约4=约12 次真实 HTTP），对降级端点放大请求量与延迟。
    if provider_name(mc) != "azure":
        return AsyncOpenAI(
            base_url=mc.base_url,
            api_key=api_key,
            max_retries=0,
            http_client=httpx.AsyncClient(http2=True),
        )
    if azure_api_mode(mc) == "v1":
        return AsyncOpenAI(
            base_url=azure_v1_base_url(mc),
            api_key=api_key,
            max_retries=0,
            http_client=httpx.AsyncClient(http2=True),
        )
    return AsyncAzureOpenAI(
        azure_endpoint=mc.base_url,
        api_key=api_key,
        api_version=getattr(mc, "api_version", None) or "",
        max_retries=0,
        http_client=httpx.AsyncClient(http2=True),
    )


# agent 路径 httpx 客户端按 model 配置缓存复用（对照 services/llm.py 的 _client_cache）：
# create_model 每回合/每个 worker 都调 make_agent_provider，若每次新建 AsyncClient 且永不关闭，
# 长跑会累积大量未关闭连接池→socket/FD 泄漏。缓存后每个配置只建一个、随进程长存。
_agent_client_cache: dict[tuple, httpx.AsyncClient] = {}


def _agent_http_client(mc: ModelConfig) -> httpx.AsyncClient:
    provider = provider_name(mc)
    mode = azure_api_mode(mc) if provider == "azure" else "openai"
    api_version = getattr(mc, "api_version", None) or ""
    key = (provider, mode, mc.base_url, api_version, mc.api_key_enc or "")
    if key not in _agent_client_cache:
        _agent_client_cache[key] = httpx.AsyncClient(http2=True)
    return _agent_client_cache[key]


def make_agent_provider(mc: ModelConfig) -> AzureProvider | OpenAIProvider:
    api_key = decrypt_api_key(mc)
    http_client = _agent_http_client(mc)
    if provider_name(mc) != "azure":
        return OpenAIProvider(base_url=mc.base_url, api_key=api_key, http_client=http_client)
    if azure_api_mode(mc) == "v1":
        return AzureProvider(
            azure_endpoint=azure_v1_base_url(mc),
            api_key=api_key,
            http_client=http_client,
        )
    return AzureProvider(
        azure_endpoint=mc.base_url,
        api_key=api_key,
        api_version=getattr(mc, "api_version", None) or "",
        http_client=http_client,
    )
