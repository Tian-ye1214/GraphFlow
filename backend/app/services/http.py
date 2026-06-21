"""通用 HTTP 取数：httpx 客户端复用 + 显式重试/退避 + 统一错误类型。仿 app/services/llm.py。
错误文案只含 method/url/status，绝不含 headers（防 token 外泄）。"""
import asyncio

import httpx

BACKOFF_BASE = 1  # 秒；重试等待 BACKOFF_BASE * 2**attempt，测试中置 0

_client_cache: dict[str, httpx.AsyncClient] = {}


class HTTPFetchError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    """复用单个 AsyncClient（连接池），避免每行重建。开 HTTP/2（与 LLM 客户端一致；
    服务端经 TLS/ALPN 支持则用 h2，否则自动回落 HTTP/1.1）。"""
    if "c" not in _client_cache:
        _client_cache["c"] = httpx.AsyncClient(http2=True)
    return _client_cache["c"]


async def fetch(method: str, url: str, headers: dict | None = None, body: str | None = None,
                timeout: int = 30, retries: int = 2) -> tuple[int, str]:
    """发一次请求，返回 (status, text)。非 2xx/网络错重试 retries 次仍失败抛 HTTPFetchError。"""
    client = _client()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.request(method, url, headers=headers or None,
                                        content=body if body else None, timeout=timeout)
            if resp.status_code >= 400:
                raise HTTPFetchError(f"HTTP {resp.status_code} {method} {url}")  # 不含 headers
            return resp.status_code, resp.text
        except HTTPFetchError as e:
            last_err = e
        except Exception as e:
            last_err = HTTPFetchError(f"请求失败 {method} {url}: {e}")  # 不含 headers
        if attempt < retries - 1:
            await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    raise last_err if last_err else HTTPFetchError(f"请求失败 {method} {url}")
