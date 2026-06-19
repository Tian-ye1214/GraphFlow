"""QC 节点输出行的两列（qc_status / qc_feedback）落库语义。"""
import asyncio
import json

from app.engine import runner
from app.services import llm as llm_mod


async def _run(auth_client, monkeypatch, session_factory, *, pass_status):
    JSONL = ('{"q": "r0"}\n{"q": "r1"}\n').encode("utf-8")
    files = [("files", ("d.jsonl", JSONL, "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    mc = (await auth_client.post("/api/models", json={
        "name": "j", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc["id"], "user_prompt": "Q:{{q}}",
                "output_column": "a", "retries": 1}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc["id"]], "model_config_id": mc["id"],
                "pass_k": 1, "user_prompt": "判:{{a}}"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
            {"source": "qc", "target": "out", "kind": "normal"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "qc列"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    async def fake_chat(mc_, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            return json.dumps({"status": pass_status, "reason": "审稿意见"}), {"prompt_tokens": 1, "completion_tokens": 1}
        return "答", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    monkeypatch.setattr(llm_mod, "BACKOFF_BASE", 0)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    for _ in range(120):
        await asyncio.sleep(0.05)
        r = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            break
    return run_id


async def test_passed_rows_carry_pass_status_and_blank_feedback(auth_client, monkeypatch, session_factory):
    run_id = await _run(auth_client, monkeypatch, session_factory, pass_status="pass")
    rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert rows and all(r["qc_status"] == "pass" and r["qc_feedback"] == "" for r in rows)


async def test_failed_rows_recorded_with_failed_status(auth_client, monkeypatch, session_factory):
    run_id = await _run(auth_client, monkeypatch, session_factory, pass_status="failed")
    # 无 rescan 边、全失败 → qc 输出为空，失败样本入 QcFailure（per-model 含 status）
    rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert rows == []
    from sqlalchemy import select
    from app.models import QcFailure
    async with session_factory() as s:
        failures = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id))).scalars().all()
    assert len(failures) == 2
    assert json.loads(failures[0].reasons_json)[0]["status"] == "failed"
