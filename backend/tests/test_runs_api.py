import asyncio
import json

from app.services import llm

GRAPH_TEMPLATE = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}
JSONL = '{"q": "问0"}\n{"q": "问1"}\n{"q": "问2"}\n'.encode("utf-8")


def patch_chat(monkeypatch, fn=None):
    async def fake(mc, system, user, params=None, retries=3):
        if fn:
            return fn(user)
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake)


async def setup_workflow(client) -> int:
    files = [("files", ("种子.jsonl", JSONL, "application/octet-stream"))]
    ds = (await client.post("/api/datasets/upload", files=files)).json()[0]
    mc = (await client.post("/api/models", json={
        "name": "m", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await client.post("/api/workflows", json={"name": "流"})).json()
    graph = json.loads(json.dumps(GRAPH_TEMPLATE))
    graph["nodes"][0]["config"]["dataset_ids"] = [ds["id"]]
    graph["nodes"][1]["config"]["model_config_id"] = mc["id"]
    await client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    return wf["id"]


async def wait_run(client, run_id, timeout=5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        r = (await client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("运行未在限期内结束")


async def test_run_end_to_end(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    assert detail["status"] == "completed"
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state == {"node_id": "gen", "status": "done", "total": 3, "done": 3, "failed": 0}
    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()
    assert rows["total"] == 1  # 批级节点 1 个工作单元
    assert len(rows["rows"]) == 3 and rows["rows"][0]["a"] == "答[Q:问0]"
    exp = await auth_client.get(f"/api/runs/{run_id}/export?format=jsonl")
    assert exp.status_code == 200
    lines = [json.loads(line) for line in exp.text.strip().splitlines()]
    assert len(lines) == 3 and lines[0]["a"] == "答[Q:问0]"
    listed = (await auth_client.get("/api/runs")).json()
    assert listed[0]["workflow_name"] == "流"


async def test_create_run_invalid_graph(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "空"})).json()
    r = await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})
    assert r.status_code == 422


async def test_create_run_foreign_dataset_rejected(auth_client):
    await auth_client.post("/api/auth/login", json={"username": "other"})
    files = [("files", ("a.jsonl", JSONL, "application/octet-stream"))]
    foreign_ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    await auth_client.post("/api/auth/login", json={"username": "tester"})
    wf_id = await setup_workflow(auth_client)
    wf = (await auth_client.get(f"/api/workflows/{wf_id}")).json()
    wf["graph"]["nodes"][0]["config"]["dataset_ids"] = [foreign_ds["id"]]
    await auth_client.put(f"/api/workflows/{wf_id}", json={"graph": wf["graph"]})
    r = await auth_client.post("/api/runs", json={"workflow_id": wf_id})
    assert r.status_code == 422


async def test_rerun_failed(auth_client, monkeypatch):
    broken = {"on": True}

    def fn(user):
        if broken["on"] and "问1" in user:
            raise RuntimeError("临时故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["failed"] == 1
    failed = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=gen&status=failed")).json()
    assert failed["rows"][0]["error"] == "临时故障"

    broken["on"] = False  # 故障修复后重跑失败行
    assert (await auth_client.post(f"/api/runs/{run_id}/rerun-failed")).status_code == 200
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["done"] == 3 and gen_state["failed"] == 0
    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()
    assert len(rows["rows"]) == 3  # 下游已重算，包含修复行


async def test_run_exposes_timing_fields(auth_client, monkeypatch):
    """运行输出须暴露 started_at/finished_at（前端据此算时长）；完成后 finished_at 非空且 >= started_at。"""
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    assert {"created_at", "started_at", "finished_at"} <= set(detail)
    assert detail["started_at"] is not None and detail["finished_at"] is not None
    assert detail["finished_at"] >= detail["started_at"]
    listed = (await auth_client.get("/api/runs")).json()
    assert {"created_at", "started_at", "finished_at"} <= set(listed[0])


async def test_rerun_failed_scoped_to_node(auth_client, monkeypatch):
    """传 node_id：只重跑该节点及其下游失败行；域外节点的失败行原样保留。"""
    # 两个 llm 节点串联：gen 失败 + 旁路另起一个失败节点，验证 scope 不波及域外。
    broken = {"gen": True}

    def fn(user):
        if "问1" in user and broken["gen"]:
            raise RuntimeError("gen故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["failed"] == 1

    broken["gen"] = False
    # 传 gen 节点重跑：应只重置 gen 及其下游
    r = await auth_client.post(f"/api/runs/{run_id}/rerun-failed?node_id=gen")
    assert r.status_code == 200
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["done"] == 3 and gen_state["failed"] == 0


async def test_rerun_failed_unknown_node_404(auth_client, monkeypatch):
    """node_id 不在该 run 图中 → 404。"""
    broken = {"on": True}

    def fn(user):
        if "问1" in user and broken["on"]:
            raise RuntimeError("故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    r = await auth_client.post(f"/api/runs/{run_id}/rerun-failed?node_id=不存在")
    assert r.status_code == 404


async def test_rerun_failed_scoped_no_failed_in_node_409(auth_client, monkeypatch):
    """指定节点的 scope 内无失败行 → 409（即便上游有失败行）。
    out 是 gen 的下游：scope={out}，gen 的失败不在其中。"""
    def fn(user):
        if "问1" in user:
            raise RuntimeError("gen故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    # 失败在 gen，但请求 out 节点（其 scope={out} 内无失败行）→ 409
    r = await auth_client.post(f"/api/runs/{run_id}/rerun-failed?node_id=out")
    assert r.status_code == 409


async def test_cancel_running(auth_client, monkeypatch):
    async def slow(mc, system, user, params=None, retries=3):
        await asyncio.sleep(0.2)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", slow)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await auth_client.post(f"/api/runs/{run_id}/cancel")
    detail = await wait_run(auth_client, run_id)
    assert detail["status"] == "cancelled"


async def test_export_node_id_path_traversal_neutralized(auth_client, monkeypatch):
    from app.config import settings

    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    exp = await auth_client.get(
        f"/api/runs/{run_id}/export?node_id=..%2F..%2F..%2Fpwned&format=jsonl")
    assert exp.status_code == 200
    assert not (settings.data_dir / "pwned.jsonl").exists()  # 未逃逸 exports 目录


async def test_export_rejects_unknown_format(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    exp = await auth_client.get(f"/api/runs/{run_id}/export?format=zip")
    assert exp.status_code == 422


async def test_create_run_rejects_qc_without_model(auth_client):
    graph = {"nodes": [
        {"id": "input_1", "type": "input", "config": {"dataset_ids": []}},
        {"id": "qc_1", "type": "qc", "config": {"user_prompt": "判:{{a}}"}},  # 缺 model_config_id
    ], "edges": [{"source": "input_1", "target": "qc_1", "kind": "normal"}]}
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    r = await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})
    assert r.status_code == 422
    assert "qc_1" in r.json()["detail"]


async def test_create_run_validates_qc_judge_models(auth_client):
    # Create a real (owned) model config so the legacy model_config_id check passes,
    # but set judge_model_ids to a nonexistent id — the new check must reject it.
    mc = (await auth_client.post("/api/models", json={
        "name": "jm", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    graph = {"nodes": [
        {"id": "input_1", "type": "input", "config": {"dataset_ids": []}},
        {"id": "qc_1", "type": "qc",
         "config": {"model_config_id": mc["id"],
                    "judge_model_ids": [999999], "pass_k": 1, "user_prompt": "判:{{a}}"}},
    ], "edges": [{"source": "input_1", "target": "qc_1", "kind": "normal"}]}
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    r = await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})
    assert r.status_code == 422
    assert "qc_1" in r.json()["detail"]


async def test_startup_resume(auth_client, monkeypatch, session_factory):
    from app.engine import manager as manager_mod
    from app.models import Run, User, Workflow, WorkflowVersion

    async with session_factory() as s:
        u = User(username="resumer")
        s.add(u)
        await s.flush()
        wf = Workflow(user_id=u.id, name="w", graph_json="{}")
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}")
        s.add(ver)
        await s.flush()
        s.add(Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="running"))
        await s.commit()

    resumed = []

    async def fake_execute(run_id, sf, sem, ev):
        resumed.append(run_id)

    monkeypatch.setattr(manager_mod, "execute_run", fake_execute)
    count = await manager_mod.resume_unfinished(session_factory)
    assert count == 1
    await asyncio.sleep(0)  # 让 create_task 调度
    assert len(resumed) == 1


async def test_run_emits_node_and_run_logs(auth_client, monkeypatch, session_factory):
    from sqlalchemy import select
    from app.models import RunLog
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    async with session_factory() as s:
        logs = (await s.execute(
            select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id))).scalars().all()
    msgs = [l.message for l in logs]
    assert any("运行开始" in m for m in msgs)
    assert any("节点 gen 开始" in m for m in msgs)
    assert any("节点 gen 完成" in m for m in msgs)
    assert any("运行结束" in m for m in msgs)


async def test_run_logs_endpoint(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    logs = (await auth_client.get(f"/api/runs/{run_id}/logs")).json()
    assert any("运行开始" in l["message"] for l in logs)
    assert all({"created_at", "node_id", "level", "message"} <= set(l) for l in logs)


async def test_run_logs_foreign_rejected(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    await auth_client.post("/api/auth/login", json={"username": "intruder"})
    r = await auth_client.get(f"/api/runs/{run_id}/logs")
    assert r.status_code == 404


async def test_delete_run_cascades(auth_client, monkeypatch, session_factory):
    from sqlalchemy import func, select
    from app.models import Run, RunLog, RunNodeState, RunRow, WorkflowVersion
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    async with session_factory() as s:
        ver_id = (await s.execute(
            select(Run.workflow_version_id).where(Run.id == run_id))).scalar()
    assert (await auth_client.delete(f"/api/runs/{run_id}")).status_code == 200
    assert (await auth_client.get(f"/api/runs/{run_id}")).status_code == 404
    async with session_factory() as s:
        for model in (RunRow, RunNodeState, RunLog):
            cnt = (await s.execute(
                select(func.count()).select_from(model).where(model.run_id == run_id))).scalar()
            assert cnt == 0
        ver = (await s.execute(
            select(func.count()).select_from(WorkflowVersion)
            .where(WorkflowVersion.id == ver_id))).scalar()
        assert ver == 0


async def test_delete_running_rejected(auth_client, monkeypatch):
    async def slow(mc, system, user, params=None, retries=3):
        await asyncio.sleep(0.3)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(llm, "chat", slow)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    assert (await auth_client.delete(f"/api/runs/{run_id}")).status_code == 409
    await auth_client.post(f"/api/runs/{run_id}/cancel")
    await wait_run(auth_client, run_id)


async def test_delete_workflow_cascades(auth_client, monkeypatch, session_factory):
    from sqlalchemy import func, select
    from app.models import (QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                            WorkflowVersion)
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    assert (await auth_client.delete(f"/api/workflows/{wf_id}")).status_code == 200
    assert (await auth_client.get(f"/api/runs/{run_id}")).status_code == 404  # run 不再是孤儿
    async with session_factory() as s:
        for Model in (RunRow, RunNodeState, RunLog, QcMetric, QcFailure):
            cnt = (await s.execute(select(func.count()).select_from(Model)
                                   .where(Model.run_id == run_id))).scalar()
            assert cnt == 0
        runs = (await s.execute(select(func.count()).select_from(Run)
                                .where(Run.workflow_id == wf_id))).scalar()
        vers = (await s.execute(select(func.count()).select_from(WorkflowVersion)
                                .where(WorkflowVersion.workflow_id == wf_id))).scalar()
        assert runs == 0 and vers == 0


async def test_delete_workflow_with_running_run_rejected(auth_client, monkeypatch):
    async def slow(mc, system, user, params=None, retries=3):
        await asyncio.sleep(0.3)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(llm, "chat", slow)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    assert (await auth_client.delete(f"/api/workflows/{wf_id}")).status_code == 409
    await auth_client.post(f"/api/runs/{run_id}/cancel")
    await wait_run(auth_client, run_id)


async def test_rerun_failed_no_duplicate_qc_metric(auth_client, monkeypatch):
    """rerun-failed 后同一 QC 节点不得出现重复指标行（否则 first_round_rate 被双算）。"""
    broken = {"on": True}

    def fn(user):
        if user.startswith("判:"):
            return json.dumps({"status": "pass", "reason": ""}), {"prompt_tokens": 1, "completion_tokens": 1}
        if broken["on"] and "问1" in user:
            raise RuntimeError("临时故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    files = [("files", ("种子.jsonl", JSONL, "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "qc流"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "Q:{{q}}", "output_column": "a", "retries": 1}},
        {"id": "qc", "type": "qc", "config": {"model_config_id": mc["id"], "user_prompt": "判:{{a}}"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await wait_run(auth_client, run_id)

    broken["on"] = False
    assert (await auth_client.post(f"/api/runs/{run_id}/rerun-failed")).status_code == 200
    await wait_run(auth_client, run_id)
    metrics = (await auth_client.get(f"/api/runs/{run_id}/qc-metrics")).json()
    assert len(metrics) == 1                                    # 单节点单条指标
    assert metrics[0]["total"] == 3 and metrics[0]["first_round_rate"] == 1.0
