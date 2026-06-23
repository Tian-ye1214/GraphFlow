
import pytest

from app.agent import turns


@pytest.fixture
async def mc_id(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "gm1", "model_name": "qwen", "base_url": "http://llm.local/v1", "api_key": "sk"})
    return r.json()["id"]


@pytest.fixture
def no_goal(monkeypatch):
    """Prevent the goal loop from actually running."""
    calls = []
    monkeypatch.setattr(turns.turn_manager, "submit_goal",
                        lambda sid, uid, wid, goal: calls.append((sid, uid, wid, goal)))
    return calls


async def test_goal_rejects_workflow_without_qc(auth_client, mc_id, no_goal):
    # 1) create an agent session (owned by tester, idle)
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    assert r.status_code == 200
    sid = r.json()["id"]

    # 2) create a workflow owned by tester with NO qc node (just an input node)
    no_qc_graph = {
        "nodes": [{"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "config": {}}],
        "edges": []
    }
    r = await auth_client.post("/api/workflows", json={"name": "no_qc_wf"})
    assert r.status_code == 200
    wf_id = r.json()["id"]
    # Update the workflow graph to the no-qc graph (API uses "graph" dict, not "graph_json")
    r = await auth_client.put(f"/api/workflows/{wf_id}", json={"name": "no_qc_wf", "graph": no_qc_graph})
    assert r.status_code == 200

    # 3) POST /api/agent/sessions/{sid}/goal -> 422 containing "质检"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": wf_id, "goal_text": "提升到 90%"})
    assert r.status_code == 422
    assert "质检" in r.json()["detail"]
    # submit_goal must NOT have been called
    assert no_goal == []


async def test_goal_rejects_empty_text(auth_client, mc_id, no_goal):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    sid = r.json()["id"]

    # Create a workflow with a QC node so we reach the empty-text check
    qc_graph = {
        "nodes": [
            {"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "qc1", "type": "qc", "position": {"x": 100, "y": 0}, "config": {}}
        ],
        "edges": []
    }
    r = await auth_client.post("/api/workflows", json={"name": "qc_wf"})
    wf_id = r.json()["id"]
    await auth_client.put(f"/api/workflows/{wf_id}", json={"name": "qc_wf", "graph": qc_graph})

    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": wf_id, "goal_text": "   "})
    assert r.status_code == 422
    assert no_goal == []


async def test_goal_rejects_running_session(auth_client, mc_id, no_goal):

    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    sid = r.json()["id"]

    # set session to running via message endpoint (no_run not patched — but no_goal is)
    # patch submit as well so message doesn't fail
    orig_submit = turns.turn_manager.submit
    calls = []
    turns.turn_manager.submit = lambda s, u, text: calls.append((s, u, text))
    try:
        r2 = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "hi"})
        assert r2.status_code == 200
    finally:
        turns.turn_manager.submit = orig_submit

    qc_graph = {
        "nodes": [
            {"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "qc1", "type": "qc", "position": {"x": 100, "y": 0}, "config": {}}
        ],
        "edges": []
    }
    r = await auth_client.post("/api/workflows", json={"name": "qc_wf2"})
    wf_id = r.json()["id"]
    await auth_client.put(f"/api/workflows/{wf_id}", json={"name": "qc_wf2", "graph": qc_graph})

    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": wf_id, "goal_text": "提升到 90%"})
    assert r.status_code == 409


async def test_goal_rejects_nonowned_workflow(auth_client, mc_id, no_goal):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    sid = r.json()["id"]

    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": 99999, "goal_text": "提升到 90%"})
    assert r.status_code == 404


async def test_goal_success_with_qc_workflow(auth_client, mc_id, no_goal):
    """A workflow that HAS a QC node returns 200 and submit_goal is called."""
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    sid = r.json()["id"]

    qc_graph = {
        "nodes": [
            {"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "qc1", "type": "qc", "position": {"x": 100, "y": 0}, "config": {}}
        ],
        "edges": []
    }
    r = await auth_client.post("/api/workflows", json={"name": "qc_ok_wf"})
    wf_id = r.json()["id"]
    await auth_client.put(f"/api/workflows/{wf_id}", json={"name": "qc_ok_wf", "graph": qc_graph})

    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": wf_id, "goal_text": "提升到 90%"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # submit_goal was called with correct args
    assert len(no_goal) == 1
    assert no_goal[0] == (sid, 1, wf_id, "提升到 90%")
