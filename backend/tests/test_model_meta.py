import pytest


@pytest.fixture(autouse=True)
def _reset_fetched():
    import app.agent.model_meta as mm
    mm._FETCHED = False
    mm._CACHE.clear()
    yield


async def test_window_from_cache(monkeypatch):
    import app.agent.model_meta as mm
    mm._CACHE.clear()
    mm._CACHE.update({"openai/gpt-x": 200000})
    assert await mm.model_window("openai/gpt-x") == 200000


async def test_window_fallback_when_unknown(monkeypatch):
    import app.agent.model_meta as mm
    mm._CACHE.clear()
    async def fake_fetch():
        return {}                      # 拉取成功但无该模型
    monkeypatch.setattr(mm, "_fetch_models", fake_fetch)
    assert await mm.model_window("nonexistent/model") == mm.DEFAULT_WINDOW


async def test_window_fallback_when_fetch_fails(monkeypatch):
    import app.agent.model_meta as mm
    mm._CACHE.clear()
    async def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(mm, "_fetch_models", boom)
    assert await mm.model_window("any") == mm.DEFAULT_WINDOW
