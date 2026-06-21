"""dry-run REST 端点：POST /api/workflows/{wf}/nodes/{node}/dry-run。404/422 契约 + 租户隔离。"""
import json

from sqlalchemy import select

from app.models import Dataset, DatasetRow, ModelConfig, User, Workflow
from app.services import llm


async def _seed_for(client, session_factory, username="tester"):
    """登录 username，建模型(API)+数据集+工作流(直插)，返回 (wf_id, mc_id)。"""
    await client.post("/api/auth/login", json={"username": username})
    mc_id = (await client.post("/api/models", json={
        "name": "m", "model_name": "qwen", "base_url": "http://x/v1", "api_key": "sk"})).json()["id"]
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == username))).scalar_one().id
        ds = Dataset(user_id=uid, name="d", row_count=2, columns_json=json.dumps(["q"]))
        s.add(ds); await s.flush()
        s.add(DatasetRow(dataset_id=ds.id, idx=0, data_json=json.dumps({"q": "你好"})))
        s.add(DatasetRow(dataset_id=ds.id, idx=1, data_json=json.dumps({"q": "世界"})))
        graph = {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "system_prompt": "译", "user_prompt": "翻:{{q}}",
                "output_column": "a"}}],
            "edges": [{"source": "in", "target": "gen", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph, ensure_ascii=False))
        s.add(wf); await s.flush()
        wf_id = wf.id
        await s.commit()
    return wf_id, mc_id


async def test_dry_run_endpoint_llm(client, session_factory, monkeypatch):
    wf_id, _ = await _seed_for(client, session_factory)

    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = await client.post(f"/api/workflows/{wf_id}/nodes/gen/dry-run", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["node_type"] == "llm_synth" and body["sampled"] == 2
    assert body["rows"][0]["rendered_user"] == "翻:你好"
    assert body["rows"][0]["output"]["a"] == "[翻:你好]"


async def test_dry_run_endpoint_render_only(client, session_factory, monkeypatch):
    wf_id, _ = await _seed_for(client, session_factory)
    called = {"n": 0}

    async def fake_chat(mc, system, user, params=None, retries=3):
        called["n"] += 1
        return "x", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = await client.post(f"/api/workflows/{wf_id}/nodes/gen/dry-run", json={"call_model": False})
    assert r.status_code == 200 and called["n"] == 0
    assert "output" not in r.json()["rows"][0]


async def test_dry_run_endpoint_override_config(client, session_factory, monkeypatch):
    wf_id, _ = await _seed_for(client, session_factory)

    async def fake_chat(mc, system, user, params=None, retries=3):
        return user, {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = await client.post(f"/api/workflows/{wf_id}/nodes/gen/dry-run",
                          json={"override_config": {"user_prompt": "改写:{{q}}"}, "call_model": False})
    assert r.json()["rows"][0]["rendered_user"] == "改写:你好"


async def test_dry_run_endpoint_not_found(client, session_factory):
    wf_id, _ = await _seed_for(client, session_factory)
    assert (await client.post("/api/workflows/99999/nodes/gen/dry-run", json={})).status_code == 404
    assert (await client.post(f"/api/workflows/{wf_id}/nodes/nope/dry-run", json={})).status_code == 404


async def test_dry_run_endpoint_dirty_config_422(client, session_factory):
    wf_id, _ = await _seed_for(client, session_factory)
    r = await client.post(f"/api/workflows/{wf_id}/nodes/gen/dry-run",
                          json={"override_config": {"fanout_n": 0}})
    assert r.status_code == 422 and "fanout_n" in r.json()["detail"]


async def test_dry_run_endpoint_tenant_isolated(client, session_factory):
    """红线：他人工作流试跑 → 404（get_owned_workflow 拦截）。"""
    wf_id, _ = await _seed_for(client, session_factory, username="alice")
    await client.post("/api/auth/login", json={"username": "bob"})
    r = await client.post(f"/api/workflows/{wf_id}/nodes/gen/dry-run", json={})
    assert r.status_code == 404
