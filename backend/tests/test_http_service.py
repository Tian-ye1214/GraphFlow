import httpx
import pytest

from app.services import http


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(http, "BACKOFF_BASE", 0)
    http._client_cache.clear()
    yield
    http._client_cache.clear()


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_status_and_text(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text='{"ok":1}')

    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    status, text = await http.fetch("GET", "http://x/api")
    assert status == 200 and text == '{"ok":1}'


async def test_fetch_post_sends_body_and_headers(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="ok")

    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    await http.fetch("POST", "http://x/api", headers={"Authorization": "Bearer T"}, body='{"q":1}')
    assert seen["body"] == '{"q":1}' and seen["auth"] == "Bearer T"


async def test_fetch_4xx_raises_without_leaking_headers(monkeypatch):
    def handler(request):
        return httpx.Response(403, text="forbidden")

    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    with pytest.raises(http.HTTPFetchError) as e:
        await http.fetch("GET", "http://x/api", headers={"Authorization": "Bearer SECRET"}, retries=2)
    msg = str(e.value)
    assert "403" in msg and "SECRET" not in msg and "Authorization" not in msg


async def test_fetch_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] >= 2 else 500, text="late-ok")

    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    status, text = await http.fetch("GET", "http://x/api", retries=3)
    assert status == 200 and calls["n"] == 2
