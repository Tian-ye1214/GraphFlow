import json
import pytest
from sqlalchemy import select
from app.agent.graph_tools import GraphToolkit
from app.models import Dataset, ModelConfig, User, Workflow


async def _seed(sf, graph=None):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="链路A",
                      graph_json=json.dumps(graph or {"nodes": [], "edges": []}))
        s.add(wf)
        s.add(ModelConfig(user_id=u.id, name="通义", model_name="qwen", base_url="http://x/v1",
                          provider="openai", api_key_enc="", default_params_json="{}"))
        s.add(Dataset(user_id=u.id, name="集A", source="upload", row_count=3,
                      columns_json=json.dumps(["q"])))
        await s.flush()
        ids = (u.id, wf.id)
        await s.commit()
    return ids


async def _graph(sf, wf_id):
    async with sf() as s:
        return json.loads((await s.get(Workflow, wf_id)).graph_json)


async def test_add_node_persists(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    msg = await GraphToolkit(sf, uid).add_node(wf_id, "llm")
    assert "llm_synth_1" in msg
    assert [n["id"] for n in (await _graph(sf, wf_id))["nodes"]] == ["llm_synth_1"]


async def test_connect_and_disconnect(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "output", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    await tk.connect_nodes(wf_id, "a", "b")
    assert (await _graph(sf, wf_id))["edges"] == [{"source": "a", "target": "b", "kind": "normal"}]
    await tk.disconnect_nodes(wf_id, "a", "b")
    assert (await _graph(sf, wf_id))["edges"] == []


async def test_set_node_config_resolves_names(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    await GraphToolkit(sf, uid).set_node_config(wf_id, "g", {"model": "通义", "out": "ans", "prompt": "答 {{q}}"})
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "g")["config"]
    assert isinstance(cfg["model_config_id"], int) and cfg["output_column"] == "ans"


async def test_set_node_config_bad_key_returns_error(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    msg = await GraphToolkit(sf, uid).set_node_config(wf_id, "g", {"nope": "x"})
    assert "未知配置键" in msg


async def test_cross_tenant_rejected(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    msg = await GraphToolkit(sf, uid + 999).add_node(wf_id, "input")
    assert msg == "工作流不存在"
    assert (await _graph(sf, wf_id))["nodes"] == []   # 受害数据未被改


async def test_add_op_and_remove(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "p", "type": "auto_process", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    await tk.add_node_op(wf_id, "p", "dedup", ["q"])
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "p")["config"]
    assert cfg["operations"] == [{"op": "dedup", "columns": ["q"]}]
    await tk.remove_node_op(wf_id, "p", 1)
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "p")["config"]
    assert cfg["operations"] == []


async def test_create_rename_delete_workflow(session_factory):
    sf = session_factory
    uid, _ = await _seed(sf)
    tk = GraphToolkit(sf, uid)
    msg = await tk.create_workflow("新链路")
    async with sf() as s:
        wf = (await s.execute(
            select(Workflow).where(Workflow.name == "新链路"))).scalars().first()
    assert wf is not None and str(wf.id) in msg
    await tk.rename_workflow(wf.id, "改名后")
    async with sf() as s:
        assert (await s.get(Workflow, wf.id)).name == "改名后"
    await tk.delete_workflow(wf.id)
    async with sf() as s:
        assert await s.get(Workflow, wf.id) is None


async def test_set_node_prompt_inline(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    await GraphToolkit(sf, uid).set_node_prompt(wf_id, "g", "user", body="翻译 {{q}}")
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "g")["config"]
    assert cfg["user_prompt"] == "翻译 {{q}}"


async def test_list_and_show(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": []}},
                   {"id": "o", "type": "output", "config": {}}],
         "edges": [{"source": "in", "target": "o", "kind": "normal"}]}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    lst = json.loads(await tk.list_workflows())
    assert any(w["id"] == wf_id and w["name"] == "链路A" for w in lst["rows"])
    shown = json.loads(await tk.show_workflow_graph(wf_id))
    assert {n["id"] for n in shown["rows"]} == {"in", "o"}
    assert len(shown["edges"]) == 1


async def test_show_cross_tenant(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    out = json.loads(await GraphToolkit(sf, uid + 999).show_workflow_graph(wf_id))
    assert out.get("error") == "workflow_not_found"


async def test_list_node_ops(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "p", "type": "auto_process",
                    "config": {"operations": [{"op": "shuffle"}]}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    out = json.loads(await GraphToolkit(sf, uid).list_node_ops(wf_id, "p"))
    assert out["rows"][0]["op"] == "shuffle"


async def test_workflow_columns_tool(session_factory):
    sf = session_factory
    # input(dataset 集A 含列 q) -> llm(产出 a) 的列血缘：llm 输入含 q、输出含 a
    g = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": []}},
                   {"id": "g", "type": "llm_synth", "config": {"output_column": "a"}}],
         "edges": [{"source": "in", "target": "g", "kind": "normal"}]}
    uid, wf_id = await _seed(sf, g)
    async with sf() as s:                              # 把 input 的 dataset_ids 指到真实的「集A」
        ds = (await s.execute(
            select(Dataset).where(Dataset.user_id == uid, Dataset.name == "集A"))).scalars().first()
        wf = await s.get(Workflow, wf_id)
        graph = json.loads(wf.graph_json)
        graph["nodes"][0]["config"]["dataset_ids"] = [ds.id]
        wf.graph_json = json.dumps(graph)
        await s.commit()
    out = json.loads(await GraphToolkit(sf, uid).workflow_columns(wf_id, "g"))
    row = out["rows"][0]
    assert row["node_id"] == "g" and "q" in row["input"] and "a" in row["output"]


async def test_workflow_columns_cross_tenant(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    out = json.loads(await GraphToolkit(sf, uid + 999).workflow_columns(wf_id))
    assert out.get("error") == "workflow_not_found"
