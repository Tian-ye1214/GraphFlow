import json
from sqlalchemy import select
from app.agent.codegen import gather_upstream_columns
from app.models import User, Workflow, Dataset, DatasetRow


async def test_gather_upstream_columns_from_dataset(auth_client, session_factory):
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["q", "category"]))
        s.add(ds); await s.flush()
        s.add(DatasetRow(dataset_id=ds.id, idx=0, data_json=json.dumps({"q": "你好", "category": "x"})))
        graph = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
                           {"id": "ap", "type": "auto_process", "config": {}}],
                 "edges": [{"source": "in", "target": "ap", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph))
        s.add(wf); await s.commit(); wf_id = wf.id
        async with session_factory() as s2:
            cols, source = await gather_upstream_columns(s2, wf_id, "ap", uid)
    assert cols == ["q", "category"] and source == "computed"


async def test_codegen_endpoint_returns_columns_no_preview(auth_client, session_factory, monkeypatch):
    async def fake_generate_code(model, instruction, columns):
        return {"code": "def process(rows):\n    return rows", "output_columns": []}
    monkeypatch.setattr("app.routers.agent.generate_code", fake_generate_code)
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        from app.models import ModelConfig
        mc = ModelConfig(user_id=uid, name="m", base_url="http://x", api_key_enc="")
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["a"]))
        s.add_all([mc, ds]); await s.flush()
        graph = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
                           {"id": "ap", "type": "auto_process", "config": {}}],
                 "edges": [{"source": "in", "target": "ap", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph))
        s.add(wf); await s.commit(); wf_id, mid = wf.id, mc.id
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wf_id, "node_id": "ap", "instruction": "删空行", "model_config_id": mid})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["a"] and body["output_columns"] == []
    assert "preview_rows" not in body and body["code"].startswith("def process")
