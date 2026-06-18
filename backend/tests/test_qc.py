import asyncio
import json

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
        return json.dumps({"status": "failed", "reason": "不是中文"}), {"prompt_tokens": 2, "completion_tokens": 3}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "译文:{{a}}"}, {"a": "hello", "_qc_reason": "旧"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False and reason == "不是中文"
    assert per_model[0]["status"] == "failed"
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


async def test_qc_judge_pass(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "pass"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is True and reason == "通过"  # 通过时 dissent 为空，返回"通过"
    assert per_model[0]["status"] == "pass"


async def test_qc_judge_status_normalized(monkeypatch):
    """status 归一：大小写/空白不敏感，"PASS"/" pass " 记为通过票。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": " PASS "}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, *_ = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is True


async def test_qc_judge_non_pass_status_fails(monkeypatch):
    """非 pass 的枚举值（如 factual_error）一律算不通过。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "factual_error", "reason": "事实错误"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False and "事实错误" in reason
    assert per_model[0]["status"] == "factual_error"  # 枚举原值保留供 jsonl 归类


async def test_qc_judge_missing_status_votes_fail(monkeypatch):
    """判定缺 status 字段：重试耗尽后判该模型「不通过」，不再抛错拖垮整个 run。"""
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)

    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"reason": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "p"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False                                            # 拿不准 → 判不过
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3}  # 3 次重试都真实调用了模型
    assert per_model[0]["status"] == "failed"


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

    call_count = {"n": 0}

    async def fake_chat(mc, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            # QC 判定：mc1 通过，mc2 不通过
            if mc.id == mc1["id"]:
                return _json.dumps({"status": "pass", "reason": "好"}), {"prompt_tokens": 1, "completion_tokens": 1}
            else:
                return _json.dumps({"status": "failed", "reason": "不合格"}), {"prompt_tokens": 1, "completion_tokens": 1}
        call_count["n"] += 1
        return f"答{call_count['n']}", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]

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
    assert metrics[0].total == 3
    assert metrics[0].first_round_pass == 0
    assert failures, "QcFailure 应写入"
    assert len(failures) == 3
    for f in failures:
        reasons = _json.loads(f.reasons_json)
        assert isinstance(reasons, list)
        assert reasons[0]["status"] == "pass" and reasons[1]["status"] == "failed"  # per-model 存 status
        sample = _json.loads(f.sample_json)
        assert "_qc_reason" not in sample and "_qc_per_model" not in sample
