"""按模型名查 OpenRouter 上下文窗口（进程内缓存，查不到回退默认）。公开端点、不带任何 key。"""
import httpx

DEFAULT_WINDOW = 128_000
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_CACHE: dict[str, int] = {}
_FETCHED = False


async def _fetch_models() -> dict[str, int]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return {m["id"]: int(m.get("context_length") or 0) for m in data if m.get("context_length")}


async def model_window(model_name: str) -> int:
    """返回模型上下文窗口 token 数；首次调用拉取并缓存，失败/查不到回退 DEFAULT_WINDOW。"""
    global _FETCHED
    if model_name in _CACHE:
        return _CACHE[model_name]
    if not _FETCHED:
        _FETCHED = True
        try:
            _CACHE.update(await _fetch_models())
        except Exception:
            pass
    return _CACHE.get(model_name, DEFAULT_WINDOW)
