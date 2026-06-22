"""刁钻数据 + 双模型质检的真实端到端测试：暴露 QC 节点的健壮性缺陷。

数据集刻意包含乱码与超长文本（1w 字符）。双模型 K-of-N 质检。
真实链路：/api/datasets/upload → /api/workflows → /api/runs（后台真实跑）。

暴露两个缺陷：
  bug①  任一判定模型对某一行返回非法内容（乱码/超长常诱发模型不吐 JSON），
        _json.loads 抛错经 judge_all 直冒到节点级 except 并 re-raise，
        execute_run 把【整个 run】判失败——已通过的行全部丢弃。
        对照 llm_synth：单行异常是 per-row 捕获（runner.py:241），run 照常完成。
  bug②  judge 返回字符串 {"pass": "false"} 时 bool("false") 为 True → 垃圾蒙混过检。
"""
import asyncio
import json

import pytest

from conftest import wait_ready

from app.engine import nodes, runner
from app.services import llm

# —— 刁钻样本 ——
LUAN_MA = "锟斤拷烫烫烫�​‮测试乱码𐍈"          # GBK 错码 + U+FFFD + 零宽 + RTL + 增补面 CJK
CHAO_CHANG = "请总结以下内容：" + "本段为压力测试文本。" * 1100   # > 10000 字符
ROWS = [
    {"q": "什么是梯度下降？"},      # idx0 正常
    {"q": "Explain overfitting."},   # idx1 正常
    {"q": LUAN_MA},                  # idx2 乱码：诱发 judge 不吐 JSON
    {"q": CHAO_CHANG},               # idx3 超长：1w 字符，验证存储/模板不崩
]

USAGE = {"prompt_tokens": 1, "completion_tokens": 1}


async def test_adversarial_dual_model_qc_one_bad_judge_must_not_nuke_run(
        auth_client, monkeypatch, session_factory):
    assert len(CHAO_CHANG) > 10000  # 确认超长样本确实 1w+ 字符

    jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ROWS).encode("utf-8")
    files = [("files", ("刁钻数据.jsonl", jsonl, "application/octet-stream"))]
    ds = await wait_ready(auth_client, (await auth_client.post("/api/datasets/upload", files=files)).json()[0]["id"])
    assert ds["row_count"] == 4  # 乱码/超长均成功入库（上传链路本身健壮）

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
                "judge_model_ids": [mc1["id"], mc2["id"]], "model_config_id": mc1["id"],
                "pass_k": 2, "user_prompt": "判断:{{a}}", "max_rounds": 1}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
            {"source": "qc", "target": "out", "kind": "normal"},
            {"source": "qc", "target": "gen", "kind": "rescan"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "刁钻质检"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    async def fake_chat(mc, system, user, params=None, retries=3):
        if params and params.get("json_mode"):                  # —— 质检判定 ——
            if "锟" in user or "�" in user:                 # 乱码行
                if mc.id == mc1["id"]:
                    return "这段内容含乱码，无法判断质量，故不返回 JSON。", USAGE  # 真实模型常见行为
                return json.dumps({"status": "failed", "reason": "乱码"}), USAGE
            return json.dumps({"status": "pass", "reason": "ok"}), USAGE  # 正常/超长行：两模型均过
        return f"答:{user}", USAGE                               # —— llm_synth：回显，保留 q 特征 ——

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)  # 乱码行判定要重试 3 次，免去退避等待

    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    run = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        run = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if run["status"] in ("completed", "failed", "cancelled"):
            break

    # —— 期望（修复后）：一行判定异常只该让那一行失败，run 必须照常完成 ——
    assert run["status"] == "completed", f"单行判定异常不应拖垮整个 run；实际：{run}"

    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    passed_q = {r["q"] for r in out_rows}
    assert passed_q == {"什么是梯度下降？", "Explain overfitting.", CHAO_CHANG}, \
        f"3 行好数据（含 1w 超长）应保留，乱码行应被隔离；实际通过：{passed_q}"

    from app.models import QcFailure, QcMetric
    from sqlalchemy import select
    async with session_factory() as s:
        metrics = (await s.execute(select(QcMetric).where(QcMetric.run_id == run_id))).scalars().all()
        failures = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id))).scalars().all()
    assert metrics and metrics[0].total == 4 and metrics[0].first_round_pass == 3
    assert len(failures) == 1 and json.loads(failures[0].sample_json)["q"] == LUAN_MA


class _MC:
    def __init__(self, id):
        self.id = id


async def test_qc_non_pass_status_must_count_as_fail(monkeypatch):
    """judge 返回非 pass 的 status（如分类失败值）必须判为不通过。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "factual_error", "reason": "明显不合格"}), USAGE

    monkeypatch.setattr(nodes.llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "垃圾内容"}, [_MC(1)], 1, asyncio.Semaphore(1))
    assert ok is False and per_model[0]["status"] == "factual_error"
