import os

import httpx
import pytest

from app.config import settings

# 测试会对本地起真实 uvicorn 服务器（test_cli/test_agent_e2e）并用 httpx 直连。
# 开 Clash 等系统代理时，httpx 默认经 urllib.getproxies 读 Windows 注册表代理，
# 把 127.0.0.1 也代理走 → 502。本地服务器永不该走代理，故对测试会话强制 NO_PROXY。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


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
