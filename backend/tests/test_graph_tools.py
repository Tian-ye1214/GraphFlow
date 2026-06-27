import json
import pytest
from sqlalchemy import func, select
from app.agent.graph_tools import GraphToolkit
from app.models import (Dataset, ModelCallLog, ModelConfig, Run, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)


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
    tk = GraphToolkit(sf, uid, confirm_delete=True)   # CRUD 直驱：删除走已确认路径
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


# --- 对抗复审修复回归 ---

async def test_set_node_config_bad_number_returns_error_not_raises(session_factory):
    """畸形数值键(conc=非数字)走 int() 抛裸 ValueError，须被 _mutate 兜成错误串而非抛进框架。"""
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    msg = await GraphToolkit(sf, uid).set_node_config(wf_id, "g", {"conc": "high"})
    assert msg.startswith("Error")
    # 失败不落库：config 未被部分写入
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "g")["config"]
    assert cfg == {}


async def test_add_node_op_bad_number_returns_error(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "p", "type": "auto_process", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    msg = await GraphToolkit(sf, uid).add_node_op(wf_id, "p", "sample", ["abc"])
    assert msg.startswith("Error")


async def test_draft_graph_missing_nodes_key_returns_error(session_factory):
    """草稿态图(经 PUT 存成缺 nodes 键)调写工具，KeyError 须兜成错误串不抛进框架。"""
    sf = session_factory
    uid, wf_id = await _seed(sf, {"edges": []})   # 真畸形：缺 nodes 键(非空 dict 不被 _seed 默认覆盖)
    msg = await GraphToolkit(sf, uid).add_node(wf_id, "llm")
    assert msg.startswith("Error")


async def test_delete_workflow_cascades_children(session_factory):
    """删工作流必须级联清子表(run/行/状态/日志/版本)零孤儿(复审揪出的批13回归)。"""
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        ver = WorkflowVersion(workflow_id=wf_id, version=1, graph_json="{}")
        s.add(ver); await s.flush()
        run = Run(user_id=uid, workflow_id=wf_id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.flush()
        s.add(RunRow(run_id=run.id, node_id="g", row_idx=0, status="done", data_json="[]"))
        s.add(RunNodeState(run_id=run.id, node_id="g", status="done"))
        s.add(ModelCallLog(user_id=uid, run_id=run.id, node_id="g", source="synth"))
        s.add(ModelCallLog(user_id=uid, workflow_id=wf_id, node_id="g", source="assistant"))
        await s.commit()
        run_id = run.id
    msg = await GraphToolkit(sf, uid, confirm_delete=True).delete_workflow(wf_id)
    assert "已删除" in msg
    async with sf() as s:
        assert await s.get(Workflow, wf_id) is None
        for Model, where in ((Run, Run.workflow_id == wf_id),
                             (RunRow, RunRow.run_id == run_id),
                             (RunNodeState, RunNodeState.run_id == run_id),
                             (WorkflowVersion, WorkflowVersion.workflow_id == wf_id),
                             (ModelCallLog, ModelCallLog.workflow_id == wf_id),
                             (ModelCallLog, ModelCallLog.run_id == run_id)):
            cnt = (await s.execute(select(func.count()).select_from(Model).where(where))).scalar()
            assert cnt == 0, f"{Model.__name__} 残留孤儿 {cnt}"


async def test_delete_workflow_blocked_while_running(session_factory):
    """有运行中的任务时删工作流应被拒(返回错误串)，工作流与数据保留。"""
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        ver = WorkflowVersion(workflow_id=wf_id, version=1, graph_json="{}")
        s.add(ver); await s.flush()
        s.add(Run(user_id=uid, workflow_id=wf_id, workflow_version_id=ver.id, status="running"))
        await s.commit()
    msg = await GraphToolkit(sf, uid, confirm_delete=True).delete_workflow(wf_id)
    assert "运行中" in msg
    async with sf() as s:
        assert await s.get(Workflow, wf_id) is not None   # 未删


async def test_delete_workflow_requires_confirmation(session_factory):
    """未确认(confirm_delete 默认 False)删工作流：返回需确认提示串且不删除/不级联——
    对齐 CLI `gf wf rm` 的服务端删除门禁，堵住原生工具绕过确认的后门。"""
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        ver = WorkflowVersion(workflow_id=wf_id, version=1, graph_json="{}")
        s.add(ver); await s.flush()
        s.add(Run(user_id=uid, workflow_id=wf_id, workflow_version_id=ver.id, status="completed"))
        await s.commit()
    msg = await GraphToolkit(sf, uid).delete_workflow(wf_id)   # 默认 confirm_delete=False
    assert "确认" in msg and f"gf wf rm {wf_id}" in msg
    async with sf() as s:
        assert await s.get(Workflow, wf_id) is not None        # 工作流未删
        cnt = (await s.execute(select(func.count()).select_from(Run)
                               .where(Run.workflow_id == wf_id))).scalar()
        assert cnt == 1                                         # 子数据完好未级联


async def test_set_node_prompt_copy_from_library(session_factory):
    """set_node_prompt mode=copy 从库复制最新版正文(复用 CatalogTools._latest_version)。"""
    from app.models import Prompt, PromptVersion
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    async with sf() as s:
        p = Prompt(user_id=uid, name="模板", description=""); s.add(p); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="旧版", variables_json="[]"))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="最新版 {{q}}", variables_json="[]"))
        pid = p.id
        await s.commit()
    await GraphToolkit(sf, uid).set_node_prompt(wf_id, "g", "user", library_ref=pid, mode="copy")
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "g")["config"]
    assert cfg["user_prompt"] == "最新版 {{q}}"


async def test_export_then_import_workflow_roundtrip(session_factory, tmp_path):
    sf = session_factory
    g = {"nodes": [{"id": "in", "type": "input", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid, workdir=tmp_path)
    msg = await tk.export_workflow(wf_id)
    assert ".gfpkg" in msg
    # 导出文件落在工作目录
    pkgs = list(tmp_path.glob("*.gfpkg"))
    assert pkgs
    imp = await tk.import_workflow(pkgs[0].name)
    assert "已导入" in imp


async def test_import_workflow_bad_file_returns_error(session_factory, tmp_path):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    (tmp_path / "bad.gfpkg").write_text("not a zip", encoding="utf-8")
    msg = await GraphToolkit(sf, uid, workdir=tmp_path).import_workflow("bad.gfpkg")
    assert msg.startswith("Error")
