from sqlalchemy import select

from app import crypto
from app.models import ModelConfig

PAYLOAD = {
    "name": "内网Qwen",
    "model_name": "qwen-max",
    "base_url": "http://10.0.0.1:8000/v1",
    "api_key": "sk-secret-123",
    "default_params": {"temperature": 0.7},
}


def test_crypto_roundtrip():
    token = crypto.encrypt("sk-abc")
    assert token != "sk-abc"
    assert crypto.decrypt(token) == "sk-abc"


async def test_create_and_list_masks_key(auth_client):
    r = await auth_client.post("/api/models", json=PAYLOAD)
    assert r.status_code == 200
    listed = (await auth_client.get("/api/models")).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "内网Qwen"
    assert listed[0]["api_key_set"] is True
    assert "sk-secret-123" not in str(listed[0])


async def test_update_without_key_keeps_old(auth_client, session_factory):
    mid = (await auth_client.post("/api/models", json=PAYLOAD)).json()["id"]
    r = await auth_client.put(f"/api/models/{mid}", json={**PAYLOAD, "api_key": "", "name": "改名"})
    assert r.status_code == 200
    async with session_factory() as s:
        mc = (await s.execute(select(ModelConfig).where(ModelConfig.id == mid))).scalar_one()
        assert crypto.decrypt(mc.api_key_enc) == "sk-secret-123"
        assert mc.name == "改名"


async def test_delete(auth_client):
    mid = (await auth_client.post("/api/models", json=PAYLOAD)).json()["id"]
    assert (await auth_client.delete(f"/api/models/{mid}")).status_code == 200
    assert (await auth_client.get("/api/models")).json() == []


async def test_user_isolation(auth_client):
    await auth_client.post("/api/models", json=PAYLOAD)
    await auth_client.post("/api/auth/login", json={"username": "other"})  # 切换用户
    assert (await auth_client.get("/api/models")).json() == []


async def test_ownership_404(auth_client):
    mid = (await auth_client.post("/api/models", json=PAYLOAD)).json()["id"]
    await auth_client.post("/api/auth/login", json={"username": "other"})  # 切换用户
    assert (await auth_client.put(f"/api/models/{mid}", json=PAYLOAD)).status_code == 404
    assert (await auth_client.delete(f"/api/models/{mid}")).status_code == 404
