import json

import pytest

from app.agent import turns
from app.auth import parse_session_cookie


@pytest.fixture
async def mc_id(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "m1", "model_name": "qwen", "base_url": "http://llm.local/v1", "api_key": "sk"})
    return r.json()["id"]


@pytest.fixture
def no_run(monkeypatch):
    calls = []
    monkeypatch.setattr(turns.turn_manager, "submit",
                        lambda sid, uid, text: calls.append((sid, uid, text)))
    return calls


async def test_create_and_get_session(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["models"] == {"coordinator": mc_id, "manager": mc_id, "worker": mc_id}
    # cli.json 已生成：server 取自请求 base_url，cookie 可验签回本人
    wd = turns.session_dir(sid)
    state = json.loads((wd / "cli.json").read_text(encoding="utf-8"))
    assert state["server"] == "http://test"
    assert parse_session_cookie(state["cookie"]) is not None
    r = await auth_client.get("/api/agent/sessions")
    assert [s["id"] for s in r.json()] == [sid]
    r = await auth_client.get(f"/api/agent/sessions/{sid}")
    assert r.json()["messages"] == []


async def test_create_session_per_role_models(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={
        "models": {"coordinator": mc_id, "manager": mc_id, "worker": mc_id}})
    assert r.status_code == 200


async def test_create_session_bad_model(auth_client, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": 999})
    assert r.status_code == 422


async def test_message_flow_and_409(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    text = "帮我搭一个翻译流水线，把 q 列翻译成英文并跑起来"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": text})
    assert r.status_code == 200
    assert no_run == [(sid, 1, text)]
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "running"
    assert detail["title"] == text[:30]
    assert detail["messages"][0]["role"] == "user"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "再来"})
    assert r.status_code == 409


async def test_stop_endpoint(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    r = await auth_client.post(f"/api/agent/sessions/{sid}/stop")
    assert r.status_code == 200
    assert sid in turns.turn_manager.stop_flags


async def test_delete_cleans_workdir(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "hi"})
    wd = turns.session_dir(sid)
    assert wd.exists()
    r = await auth_client.delete(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200
    assert not wd.exists()
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404


async def test_cross_user_isolation(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404
    assert (await auth_client.post(f"/api/agent/sessions/{sid}/messages",
                                   json={"text": "x"})).status_code == 404
    assert (await auth_client.get("/api/agent/sessions")).json() == []
