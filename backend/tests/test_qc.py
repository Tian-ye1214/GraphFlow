import asyncio
import json

from conftest import wait_ready

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
    ds = await wait_ready(auth_client, (await auth_client.post("/api/datasets/upload", files=files)).json()[0]["id"])
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


async def test_qc_resume_does_not_double_count_metrics(auth_client, monkeypatch, session_factory):
    """崩溃-续跑经过未完成的 QC 节点不重复累计 QcMetric/QcFailure。
    回归:首轮 QcMetric 在回扫循环前落库、done 标记在循环后才写;若崩溃在中间窗口,resume 重跑本节点体
    会再 INSERT 一条,污染 first_round_rate(可 >1)。修复=重算前按 (run_id,node_id) 幂等清旧指标/失败样本。"""
    import asyncio as _aio
    import json as _json

    from sqlalchemy import select, update as _sa_update
    from app.engine import runner
    from app.engine.graph import parse_graph
    from app.models import QcFailure, QcMetric, Run, RunRow
    from app.services import llm as llm_mod

    JSONL = ('{"q": "r0"}\n{"q": "r1"}\n{"q": "r2"}\n').encode("utf-8")
    files = [("files", ("data.jsonl", JSONL, "application/octet-stream"))]
    ds = await wait_ready(auth_client, (await auth_client.post(
        "/api/datasets/upload", files=files)).json()[0]["id"])
    mc = (await auth_client.post("/api/models", json={
        "name": "judge", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc["id"], "user_prompt": "Q:{{q}}",
                "output_column": "a", "concurrency": 4, "retries": 1}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc["id"]], "model_config_id": mc["id"],
                "pass_k": 1, "user_prompt": "判断:{{a}}"}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "qc续跑"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    async def fake_chat(mc_, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            return _json.dumps({"status": "pass", "reason": "好"}), {"prompt_tokens": 1, "completion_tokens": 1}
        return "答", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    for _ in range(200):
        await _aio.sleep(0.05)
        if (await auth_client.get(f"/api/runs/{run_id}")).json()["status"] in (
                "completed", "failed", "cancelled"):
            break

    async with session_factory() as s:
        user_id = (await s.get(Run, run_id)).user_id
        metrics = (await s.execute(select(QcMetric).where(
            QcMetric.run_id == run_id, QcMetric.node_id == "qc"))).scalars().all()
        assert len(metrics) == 1 and metrics[0].total == 3 and metrics[0].first_round_pass == 3
        gen_rows = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen").order_by(RunRow.row_idx))).scalars().all()
        inputs = [r for rr in gen_rows for r in _json.loads(rr.data_json)]
        # 模拟"崩溃在回扫窗口":再塞一条陈旧 QcMetric/QcFailure + 抹掉本节点 done 标记(强制重跑节点体)
        s.add(QcMetric(run_id=run_id, node_id="qc", total=99, first_round_pass=99))
        s.add(QcFailure(run_id=run_id, node_id="qc", trace_id="stale",
                        sample_json="{}", reasons_json="[]"))
        await s.execute(_sa_update(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "qc", RunRow.row_idx == 0).values(status="pending"))
        await s.commit()

    g = parse_graph(graph)
    qc_node = next(n for n in g.nodes if n.id == "qc")
    await runner._run_qc_node(session_factory, run_id, user_id, g, qc_node, inputs,
                             _aio.Semaphore(4), _aio.Event())

    async with session_factory() as s:
        metrics = (await s.execute(select(QcMetric).where(
            QcMetric.run_id == run_id, QcMetric.node_id == "qc"))).scalars().all()
        failures = (await s.execute(select(QcFailure).where(
            QcFailure.run_id == run_id, QcFailure.node_id == "qc"))).scalars().all()
    # 修复前:陈旧+新=2 条 QcMetric、陈旧 QcFailure 残留;修复后:幂等清旧 → QcMetric 1 条、QcFailure 0 条
    assert len(metrics) == 1, f"QcMetric 重复累计:{[(m.total, m.first_round_pass) for m in metrics]}"
    assert metrics[0].total == 3 and metrics[0].first_round_pass == 3
    assert len(failures) == 0
