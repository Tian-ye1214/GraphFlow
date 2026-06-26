import asyncio

import pytest

from app.agent.turns import AgentTurnManager


class StubTM:
    def __init__(self, result):
        self.result = result
        self.cancelled = []

    def cancel(self, sid):
        self.cancelled.append(sid)
        return self.result


async def _make_session(auth_client):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    sess = (await auth_client.post("/api/agent/sessions",
                                   json={"model_config_id": mc["id"]})).json()
    return sess["id"]


async def test_interrupt_writes_marker_when_cancelled(auth_client, monkeypatch):
    sid = await _make_session(auth_client)
    stub = StubTM(True)
    monkeypatch.setattr("app.routers.agent.turn_manager", stub)
    r = await auth_client.post(f"/api/agent/sessions/{sid}/interrupt")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "interrupted": True}
    assert stub.cancelled == [sid]
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["messages"][-1]["role"] == "assistant"
    assert detail["messages"][-1]["content"]["text"] == "（已被用户打断）"


async def test_interrupt_no_marker_when_idle(auth_client, monkeypatch):
    sid = await _make_session(auth_client)
    stub = StubTM(False)
    monkeypatch.setattr("app.routers.agent.turn_manager", stub)
    r = await auth_client.post(f"/api/agent/sessions/{sid}/interrupt")
    assert r.json() == {"ok": True, "interrupted": False}
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert all(m["content"].get("text") != "（已被用户打断）" for m in detail["messages"])


async def test_interrupt_unknown_session_404(auth_client):
    r = await auth_client.post("/api/agent/sessions/999999/interrupt")
    assert r.status_code == 404


async def _make_model_and_wf(auth_client, node_id, node_type):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": {
        "nodes": [{"id": node_id, "type": node_type, "config": {}}], "edges": []}})
    return mc, wf


async def test_node_assist_stop_cancels_inflight(auth_client, monkeypatch):
    started = asyncio.Event()

    async def blocking_cfg(*a, **k):
        started.set()
        await asyncio.Event().wait()        # 永久阻塞，直到被取消

    monkeypatch.setattr("app.agent.codegen.generate_node_config", blocking_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    call_id = "call-xyz"
    post = asyncio.create_task(auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "x", "model_config_id": mc["id"], "call_id": call_id}))
    await asyncio.wait_for(started.wait(), 5)   # 确保在途且已注册
    r2 = await auth_client.post("/api/agent/node-assist/stop", json={"call_id": call_id})
    assert r2.status_code == 200
    r1 = await asyncio.wait_for(post, 5)
    assert r1.status_code == 200
    assert r1.json()["cancelled"] is True
    assert r1.json()["config"] is None


async def test_node_assist_without_callid_still_works(auth_client, monkeypatch):
    async def fake_cfg(*a, **k):
        return {"reply": "ok", "config": None}

    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "x", "model_config_id": mc["id"]})   # 无 call_id
    assert r.status_code == 200
    assert r.json()["reply"] == "ok"


async def test_node_assist_stop_unknown_callid_ok(auth_client):
    r = await auth_client.post("/api/agent/node-assist/stop", json={"call_id": "ghost"})
    assert r.status_code == 200      # 找不到也优雅返回（幂等）
