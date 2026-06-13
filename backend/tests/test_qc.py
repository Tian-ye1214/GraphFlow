import asyncio
import json

import pytest

from app.engine import nodes
from app.services import llm


class _FakeMC:
    """最小 ModelConfig 替身，仅需 id 属性即可通过 nodes.run_qc_judge_row 内的 mc.id 访问。"""
    def __init__(self, id=1):
        self.id = id


async def test_qc_judge_parses_verdict(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        assert params and params.get("json_mode") is True  # 判定强制 json 模式
        assert "hello" in user  # 用 base 渲染（剥离 _qc_reason）
        return json.dumps({"pass": False, "reason": "不是中文"}), {"prompt_tokens": 2, "completion_tokens": 3}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "译文:{{a}}"}, {"a": "hello", "_qc_reason": "旧"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False and reason == "不是中文"
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


async def test_qc_judge_pass(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"pass": True}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is True and reason == "通过"  # 通过时 dissent 为空，返回"通过"


async def test_qc_judge_missing_pass_raises(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"reason": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    with pytest.raises(ValueError):
        await nodes.run_qc_judge_row({"user_prompt": "p"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))


async def test_qc_multi_model_metric_and_failures(auth_client, monkeypatch, session_factory):
    """两个判定模型 pass_k=2；部分行仅 1/2 通过 → QcFailure 落库；首轮指标 → QcMetric 落库。"""
    import json as _json

    from app.services import llm as llm_mod

    JSONL = ('{"q": "r0"}\n{"q": "r1"}\n{"q": "r2"}\n').encode("utf-8")
    files = [("files", ("data.jsonl", JSONL, "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    mc1 = (await auth_client.post("/api/models", json={
        "name": "judge1", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k1", "default_params": {}})).json()
    mc2 = (await auth_client.post("/api/models", json={
        "name": "judge2", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k2", "default_params": {}})).json()

    # 工作流：input -> llm_synth(mc1) -> qc(judge_model_ids=[mc1,mc2], pass_k=2)
    # model_config_id=mc1["id"] 让 runs.py 资源校验通过（backward compat）
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc1["id"], "user_prompt": "Q:{{q}}",
                "output_column": "a", "concurrency": 4, "retries": 1}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc1["id"], mc2["id"]],
                "model_config_id": mc1["id"],  # 供 runs.py 资源校验
                "pass_k": 2,
                "user_prompt": "判断:{{a}}"}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "qc多模型"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    # mock: llm_synth 调用（无 json_mode）返回纯文本；QC 判定调用（json_mode=True）按模型 id 分别返回
    # mc1 始终 pass=True；mc2 始终 pass=False → 每行 1/2 < pass_k=2 → 全部失败
    call_count = {"n": 0}

    async def fake_chat(mc, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            # QC 判定：mc1 通过，mc2 不通过
            if mc.id == mc1["id"]:
                return _json.dumps({"pass": True, "reason": "好"}), {"prompt_tokens": 1, "completion_tokens": 1}
            else:
                return _json.dumps({"pass": False, "reason": "不合格"}), {"prompt_tokens": 1, "completion_tokens": 1}
        # llm_synth
        call_count["n"] += 1
        return f"答{call_count['n']}", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]

    # 等待运行完成
    for _ in range(100):
        await asyncio.sleep(0.05)
        r = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            break

    from sqlalchemy import select
    from app.models import QcFailure, QcMetric
    async with session_factory() as s:
        metrics = (await s.execute(select(QcMetric).where(QcMetric.run_id == run_id))).scalars().all()
        failures = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id))).scalars().all()

    assert metrics, "QcMetric 应写入"
    assert all(0 <= m.first_round_pass <= m.total for m in metrics)
    assert metrics[0].total == 3  # 3 行输入
    # mc2 全部不通过 → 1/2 < pass_k=2 → 所有行失败
    assert metrics[0].first_round_pass == 0
    assert failures, "QcFailure 应写入"
    assert len(failures) == 3  # 3 行全部失败
    for f in failures:
        reasons = _json.loads(f.reasons_json)
        assert isinstance(reasons, list)
        sample = _json.loads(f.sample_json)
        assert "_qc_reason" not in sample and "_qc_per_model" not in sample
