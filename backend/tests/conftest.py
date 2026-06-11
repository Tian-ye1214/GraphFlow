import httpx
import pytest

from app.config import settings


@pytest.fixture
async def session_factory(client):
    from app import db
    return db.get_session_factory()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app import events
    events.subscribers.clear()
    from app import db
    await db.init_db()
    from app.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.engine.dispose()


@pytest.fixture
async def auth_client(client):
    await client.post("/api/auth/login", json={"username": "tester"})
    return client
