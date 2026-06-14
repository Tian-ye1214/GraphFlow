import json

from sqlalchemy import select

from app.models import Dataset, User, Workflow


async def test_workflow_columns_endpoint(auth_client, session_factory):
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["id", "q", "category"]))
        s.add(ds)
        await s.flush()
        graph = {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
            {"id": "ls", "type": "llm_synth", "config": {"output_mode": "json", "output_columns": ["q_en"]}},
            {"id": "qc", "type": "qc", "config": {}}],
            "edges": [{"source": "in", "target": "ls", "kind": "normal"},
                      {"source": "ls", "target": "qc", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph))
        s.add(wf)
        await s.commit()
        wf_id = wf.id
    r = await auth_client.get(f"/api/workflows/{wf_id}/columns")
    assert r.status_code == 200
    body = r.json()
    assert body["ls"]["output"] == ["id", "q", "category", "q_en"]
    assert body["qc"]["input"] == ["id", "q", "category", "q_en"]


async def test_workflow_columns_404_foreign(auth_client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=999, name="other", graph_json=json.dumps({"nodes": [], "edges": []}))
        s.add(wf)
        await s.commit()
        wf_id = wf.id
    r = await auth_client.get(f"/api/workflows/{wf_id}/columns")
    assert r.status_code == 404


async def test_workflow_columns_invalid_graph_returns_422(auth_client):
    """草稿态非法图（有环 / 悬空边）属正常编辑中间态：列接口应给 422 而非 500。"""
    wf = (await auth_client.post("/api/workflows", json={"name": "草稿"})).json()
    cyclic = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                        {"id": "b", "type": "llm_synth", "config": {}}],
              "edges": [{"source": "a", "target": "b", "kind": "normal"},
                        {"source": "b", "target": "a", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": cyclic})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/columns")).status_code == 422
    dangling = {"nodes": [{"id": "a", "type": "input", "config": {}}],
                "edges": [{"source": "a", "target": "ghost", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": dangling})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/columns")).status_code == 422
