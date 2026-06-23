import json

from app.models import ModelCallLog, QcFailure, Run, RunRow, User, WorkflowVersion
from app.services import llm
from tests.test_runs_api import wait_run


async def _owned_run(session_factory):
    async with session_factory() as s:
        uid = (await s.execute(
            __import__("sqlalchemy").select(User).where(User.username == "tester")
        )).scalar_one().id
        ver = WorkflowVersion(workflow_id=1, version=1, graph_json=json.dumps({
            "nodes": [
                {"id": "in", "type": "input", "config": {}},
                {"id": "gen", "type": "llm_synth", "config": {}},
                {"id": "qc", "type": "qc", "config": {}},
                {"id": "out", "type": "output", "config": {}},
            ],
            "edges": [
                {"source": "in", "target": "gen", "kind": "normal"},
                {"source": "gen", "target": "qc", "kind": "normal"},
                {"source": "qc", "target": "out", "kind": "normal"},
            ],
        }, ensure_ascii=False))
        s.add(ver)
        await s.flush()
        run = Run(user_id=uid, workflow_id=1, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.flush()
        await s.commit()
        return run.id


async def test_trace_api_joins_rows_qc_failures_and_model_logs(auth_client, session_factory):
    run_id = await _owned_run(session_factory)
    trace_id = "tr-root-1"
    async with session_factory() as s:
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=0, status="done",
                     trace_id=trace_id,
                     data_json=json.dumps([{"q": "原题", "a": "坏答案",
                                            "_gf_trace_id": trace_id}], ensure_ascii=False),
                     prompt_tokens=3, completion_tokens=4))
        s.add(QcFailure(run_id=run_id, node_id="qc", trace_id=trace_id,
                        sample_json=json.dumps({"q": "原题", "a": "坏答案",
                                                "_gf_trace_id": trace_id}, ensure_ascii=False),
                        reasons_json=json.dumps([
                            {"model_config_id": 1, "status": "failed", "reason": "事实错误"}
                        ], ensure_ascii=False)))
        s.add(ModelCallLog(user_id=1, run_id=run_id, node_id="gen", source="synth",
                           trace_id=trace_id, model_name="m", provider="openai",
                           request_json=json.dumps([{"role": "user", "content": "Q"}]),
                           response_json="坏答案", prompt_tokens=3, completion_tokens=4))
        await s.commit()

    resp = await auth_client.get(f"/api/runs/{run_id}/trace/{trace_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == trace_id
    gen = next(e for e in body["events"] if e["node_id"] == "gen")
    assert gen["status"] == "done"
    assert gen["tokens"] == {"prompt_tokens": 3, "completion_tokens": 4}
    assert gen["model_logs"][0]["response"] == "坏答案"
    qc = next(e for e in body["events"] if e["node_id"] == "qc")
    assert qc["qc_reasons"][0]["reason"] == "事实错误"
    assert "_gf_trace_id" not in gen["output"][0]


async def test_trace_api_includes_parent_trace_model_logs_for_fanout_child(auth_client,
                                                                           session_factory):
    run_id = await _owned_run(session_factory)
    parent_trace = "root-trace"
    child_trace = "root-trace|gen:0:1"
    async with session_factory() as s:
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=0, status="done",
                     trace_id=child_trace,
                     data_json=json.dumps([{"q": "x", "a": "child",
                                            "_gf_trace_id": child_trace,
                                            "_gf_parent_trace_id": parent_trace}])))
        s.add(ModelCallLog(user_id=1, run_id=run_id, node_id="gen", source="synth",
                           trace_id=parent_trace, model_name="m", provider="openai",
                           request_json='[{"role":"user","content":"Q"}]',
                           response_json="parent response"))
        await s.commit()

    resp = await auth_client.get(f"/api/runs/{run_id}/trace/{child_trace}")
    assert resp.status_code == 200
    gen = next(e for e in resp.json()["events"] if e["node_id"] == "gen")
    assert gen["model_logs"][0]["response"] == "parent response"


async def test_failed_rows_and_qc_failures_expose_trace_id(auth_client, session_factory):
    run_id = await _owned_run(session_factory)
    async with session_factory() as s:
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=2, status="failed",
                     trace_id="tr-fail", error="boom"))
        s.add(QcFailure(run_id=run_id, node_id="qc", trace_id="tr-qc",
                        sample_json='{"q":"x"}', reasons_json='[]'))
        await s.commit()

    failed = (await auth_client.get(
        f"/api/runs/{run_id}/rows?node_id=gen&status=failed")).json()
    assert failed["rows"][0]["trace_id"] == "tr-fail"
    qcf = (await auth_client.get(f"/api/runs/{run_id}/qc-failures")).json()
    assert qcf[0]["trace_id"] == "tr-qc"


async def test_run_rows_export_and_qc_jsonl_hide_internal_trace_fields(auth_client, session_factory):
    run_id = await _owned_run(session_factory)
    trace_id = "tr-hidden"
    async with session_factory() as s:
        s.add(RunRow(run_id=run_id, node_id="out", row_idx=0, status="done",
                     trace_id=trace_id,
                     data_json=json.dumps([{"q": "x", "a": "y",
                                            "_gf_trace_id": trace_id,
                                            "_gf_parent_trace_id": "parent"}])))
        s.add(QcFailure(run_id=run_id, node_id="qc", trace_id=trace_id,
                        sample_json=json.dumps({"q": "x", "_gf_trace_id": trace_id}),
                        reasons_json=json.dumps([{"model_config_id": 1, "status": "failed",
                                                  "reason": "bad"}])))
        await s.commit()

    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()["rows"]
    assert rows == [{"q": "x", "a": "y"}]
    export = await auth_client.get(f"/api/runs/{run_id}/export?node_id=out&format=jsonl")
    assert "_gf_trace_id" not in export.text and "_gf_parent_trace_id" not in export.text
    qcf = await auth_client.get(f"/api/runs/{run_id}/qc-failures.jsonl")
    assert "_gf_trace_id" not in qcf.text


async def test_runner_assigns_trace_ids_and_hides_internal_fields(auth_client, monkeypatch,
                                                                  session_factory):
    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"答:{user}", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    upload = await auth_client.post(
        "/api/datasets/upload",
        files=[("files", ("seed.jsonl", b'{"q":"a"}\n{"q":"b"}\n', "application/octet-stream"))])
    ds_id = upload.json()[0]["id"]
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "m", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "trace-run"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "{{q}}", "output_column": "a",
            "fanout_n": 2}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "out", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await wait_run(auth_client, run_id)

    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()["rows"]
    assert len(rows) == 4
    assert all("_gf_trace_id" not in r and "_gf_parent_trace_id" not in r for r in rows)
    async with session_factory() as s:
        gen_rows = (await s.execute(__import__("sqlalchemy").select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen"))).scalars().all()
    traces = [row["_gf_trace_id"] for rec in gen_rows for row in json.loads(rec.data_json)]
    assert len(traces) == len(set(traces)) == 4
    trace = (await auth_client.get(f"/api/runs/{run_id}/trace/{traces[0]}")).json()
    assert [e["node_id"] for e in trace["events"]] == ["gen", "out"]
    assert trace["parent_trace_id"].startswith(f"run{run_id}:in:")


async def test_trace_internal_fields_are_not_visible_to_node_prompts(auth_client, monkeypatch):
    seen_users = []

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen_users.append(user)
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake_chat)
    upload = await auth_client.post(
        "/api/datasets/upload",
        files=[("files", ("seed.jsonl", b'{"q":"a"}\n', "application/octet-stream"))])
    ds_id = upload.json()[0]["id"]
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "m", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "trace-prompt"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"],
            "user_prompt": "trace={{_gf_trace_id}} parent={{_gf_parent_trace_id}} q={{q}}",
            "output_column": "a"}},
    ], "edges": [{"source": "in", "target": "gen", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await wait_run(auth_client, run_id)

    assert seen_users == ["trace= parent= q=a"]


async def test_runner_qc_failure_trace_links_to_trace_api(auth_client, monkeypatch):
    async def fake_chat(mc, system, user, params=None, retries=3):
        if "判定" in user:
            return json.dumps({"status": "failed", "reason": "不满足要求"}), {
                "prompt_tokens": 1, "completion_tokens": 1}
        return "bad", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake_chat)
    upload = await auth_client.post(
        "/api/datasets/upload",
        files=[("files", ("seed.jsonl", b'{"q":"a"}\n', "application/octet-stream"))])
    ds_id = upload.json()[0]["id"]
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "m", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "trace-qc"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "{{q}}", "output_column": "a"}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": mc["id"], "user_prompt": "判定 {{a}}", "max_rounds": 0}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"}]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await wait_run(auth_client, run_id)

    failures = (await auth_client.get(f"/api/runs/{run_id}/qc-failures")).json()
    assert failures[0]["trace_id"]
    trace = (await auth_client.get(
        f"/api/runs/{run_id}/trace/{failures[0]['trace_id']}")).json()
    qc_event = next(e for e in trace["events"] if e["node_id"] == "qc")
    assert qc_event["qc_reasons"][0]["reason"] == "不满足要求"
