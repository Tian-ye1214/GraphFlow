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


# --- 大文件后台摄入：上传端点改为返回 importing 占位 + 后台写分片。测试需轮询到 ready。----

async def wait_status(client, ds_id, tries=400):
    """轮询某数据集摄入到终态(ready/failed)，返回其 dict。"""
    import asyncio
    for _ in range(tries):
        body = (await client.get(f"/api/datasets/{ds_id}")).json()
        if body.get("status") != "importing":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"dataset {ds_id} 摄入未在限时内完成")


async def wait_ready(client, ds_id, tries=400):
    """轮询某数据集摄入到 ready 返回其 dict；failed/超时报错。"""
    body = await wait_status(client, ds_id, tries)
    assert body.get("status") == "ready", body
    return body


async def upload_ready(client, name, content):
    """上传单文件并等待全部产出数据集 ready，返回 ready 后的 dict 列表（与旧端点同形）。"""
    r = await client.post(
        "/api/datasets/upload",
        files=[("files", (name, content, "application/octet-stream"))])
    assert r.status_code == 200, r.text
    return [await wait_ready(client, ph["id"]) for ph in r.json()]
