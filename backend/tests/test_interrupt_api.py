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
