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


async def test_workflow_columns_dirty_config_returns_422(auth_client):
    """脏 config 形状（WorkflowUpdate.graph 只校验顶层 dict，节点内部可存任意脏值）：列接口 422 而非 500。"""
    wf = (await auth_client.post("/api/workflows", json={"name": "脏"})).json()
    cases = [
        {"nodes": [{"id": "in", "type": "input", "config": "oops"}], "edges": []},           # config 非 dict
        {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": 7}}], "edges": []},  # dataset_ids 非 list
        {"nodes": [{"id": "ap", "type": "auto_process", "config": {"operations": ["bogus"]}}], "edges": []},  # op 非 dict
    ]
    for g in cases:
        await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": g})
        r = await auth_client.get(f"/api/workflows/{wf['id']}/columns")
        assert r.status_code == 422, f"{g} 应 422，实得 {r.status_code}"


async def test_workflow_columns_dirty_rename_mapping_returns_422(auth_client, session_factory):
    """auto_process rename mapping 写成 list（非 dict）且有真实上游列 → 列接口 422 而非 500（#9）。"""
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["a", "b", "c"]))
        s.add(ds); await s.commit(); dsid = ds.id
    wf = (await auth_client.post("/api/workflows", json={"name": "脏rename"})).json()
    g = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [dsid]}},
                   {"id": "ap", "type": "auto_process",
                    "config": {"operations": [{"op": "rename", "mapping": ["a", "b"]}]}}],
         "edges": [{"source": "in", "target": "ap", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": g})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/columns")).status_code == 422
