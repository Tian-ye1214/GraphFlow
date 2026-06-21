"""NodeInfoTools 直接单测：图拓扑 / 运行汇总 / 本节点产出·失败行。"""
import json

from app.agent.node_info import NodeInfoTools
from app.models import (ModelCallLog, QcFailure, Run, RunNodeState, RunRow, User, Workflow,
                        WorkflowVersion)

GRAPH = json.dumps({
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 1, "output_column": "a",
                    "system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}"}},
        {"id": "qc", "type": "qc",
         "config": {"judge_model_ids": [1, 2], "pass_k": 2, "status_column": "verdict",
                    "system_prompt": "判定员", "user_prompt": "判:{{a}}"}},
        {"id": "out", "type": "output", "config": {"save_as_dataset": True, "dataset_name": "结果"}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "qc", "kind": "normal"},
              {"source": "qc", "target": "out", "kind": "normal"},
              {"source": "qc", "target": "gen", "kind": "rescan"}],
})


async def _seed(sf):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="流", graph_json=GRAPH); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=GRAPH); s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed",
                  stats_json=json.dumps({"prompt_tokens": 7, "completion_tokens": 9}))
        s.add(run); await s.flush()
        for i in range(2):   # gen 2 条 done
            s.add(RunRow(run_id=run.id, node_id="gen", row_idx=i, status="done",
                         data_json=json.dumps([{"q": "x", "a": f"答{i}"}])))
        s.add(RunRow(run_id=run.id, node_id="gen", row_idx=2, status="failed",
                     attempt=3, error="模型超时"))
        s.add(RunNodeState(run_id=run.id, node_id="gen", status="failed", total=3, done=2, failed=1))
        ids = (u.id, wf.id, run.id)
        await s.commit()
    return ids


async def test_show_workflow_graph(session_factory):
    sf = session_factory
    uid, wf_id, _ = await _seed(sf)
    out = json.loads(await NodeInfoTools(sf, uid, wf_id, "qc").show_workflow_graph())
    assert out["workflow_name"] == "流" and out["current_node_id"] == "qc"
    nodes = {n["id"]: n for n in out["rows"]}
    assert nodes["gen"]["type"] == "llm_synth" and "翻译" in nodes["gen"]["config"]["system_prompt"]
    assert nodes["qc"]["config"]["pass_k"] == 2 and nodes["qc"]["config"]["status_column"] == "verdict"
    kinds = {(e["source"], e["target"]): e["kind"] for e in out["edges"]}
    assert kinds[("qc", "gen")] == "rescan" and kinds[("gen", "qc")] == "normal"


async def test_latest_run_summary(session_factory):
    sf = session_factory
    uid, wf_id, run_id = await _seed(sf)
    out = json.loads(await NodeInfoTools(sf, uid, wf_id, "gen").latest_run_summary())
    assert out["run_id"] == run_id and out["status"] == "completed"
    assert out["stats"]["completion_tokens"] == 9
    states = {r["node_id"]: r for r in out["rows"]}
    assert states["gen"]["done"] == 2 and states["gen"]["failed"] == 1


async def test_read_node_output_done_and_failed(session_factory):
    sf = session_factory
    uid, wf_id, _ = await _seed(sf)
    done = json.loads(await NodeInfoTools(sf, uid, wf_id, "gen").read_node_output("done", 5))
    assert len(done["rows"]) == 2 and done["rows"][0]["a"] == "答0"
    failed = json.loads(await NodeInfoTools(sf, uid, wf_id, "gen").read_node_output("failed", 5))
    assert failed["rows"][0]["error"] == "模型超时" and failed["rows"][0]["attempt"] == 3


async def test_read_qc_failures(session_factory):
    sf = session_factory
    uid, wf_id, run_id = await _seed(sf)
    async with sf() as s:
        s.add(QcFailure(run_id=run_id, node_id="qc", sample_json=json.dumps({"a": "差译文"}),
                        reasons_json=json.dumps([{"model_config_id": 1, "status": "failed",
                                                  "reason": "不达标"}])))
        await s.commit()
    out = json.loads(await NodeInfoTools(sf, uid, wf_id, "qc").read_qc_failures())
    assert out["run_id"] == run_id and out["rows"][0]["sample"]["a"] == "差译文"
    assert out["rows"][0]["reasons"][0]["reason"] == "不达标"


async def test_read_node_model_logs(session_factory):
    sf = session_factory
    uid, wf_id, run_id = await _seed(sf)
    async with sf() as s:
        s.add(ModelCallLog(user_id=uid, run_id=run_id, node_id="gen", source="synth",
                           model_name="qwen",
                           request_json=json.dumps([{"role": "user", "content": "翻译:你好"}]),
                           response_json="hello", prompt_tokens=3, completion_tokens=2))
        await s.commit()
    out = json.loads(await NodeInfoTools(sf, uid, wf_id, "gen").read_node_model_logs())
    assert out["rows"][0]["response"] == "hello"
    assert out["rows"][0]["request"][0]["content"] == "翻译:你好"
    assert out["rows"][0]["completion_tokens"] == 2


async def test_node_info_tenant_isolated(session_factory):
    sf = session_factory
    uid, wf_id, _ = await _seed(sf)
    out = json.loads(await NodeInfoTools(sf, uid + 999, wf_id, "gen").show_workflow_graph())
    assert out.get("error") == "workflow_not_found"   # 他人不得读本工作流图
