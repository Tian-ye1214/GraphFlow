import json
import pytest
from app.models import ModelConfig, User, Workflow
from app.services import graph_ops as go
from app.services.workflow_store import resolve_ref, update_workflow_graph


async def _seed(sf):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="链路A", graph_json=json.dumps({"nodes": [], "edges": []}))
        s.add(wf)
        s.add(ModelConfig(user_id=u.id, name="通义", model_name="qwen", base_url="http://x/v1",
                          provider="openai", api_key_enc="", default_params_json="{}"))
        await s.flush()
        ids = (u.id, wf.id)
        await s.commit()
    return ids


async def test_update_workflow_graph_persists(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        wf = await s.get(Workflow, wf_id)
        graph = json.loads(wf.graph_json)
        go.add_node(graph, "input", "in")
        await update_workflow_graph(s, wf, graph)
    async with sf() as s:
        wf = await s.get(Workflow, wf_id)
        assert [n["id"] for n in json.loads(wf.graph_json)["nodes"]] == ["in"]


async def test_resolve_ref_by_id_and_name(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        assert await resolve_ref(s, uid, "workflows", str(wf_id)) == wf_id
        mid = await resolve_ref(s, uid, "models", "通义")
        assert isinstance(mid, int)


async def test_resolve_ref_missing_raises(session_factory):
    sf = session_factory
    uid, _ = await _seed(sf)
    async with sf() as s:
        with pytest.raises(go.GraphOpError, match="找不到"):
            await resolve_ref(s, uid, "models", "不存在的模型")


async def test_resolve_ref_cross_tenant_id_rejected(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        with pytest.raises(go.GraphOpError, match="找不到"):
            await resolve_ref(s, uid + 999, "workflows", str(wf_id))
