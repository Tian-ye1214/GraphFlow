"""列名压测 / 刁钻列名真实端到端测试：暴露列处理健壮性缺陷。

真实链路：/api/datasets/upload → /api/workflows(+/columns 血缘) → /api/runs（后台真实跑）。

覆盖：
  - bug：用户列名以 `_qc` 开头（如 `_qc_score`）被 startswith("_qc") 静默吃掉——
    内部只该剔除运行期注入的 `_qc_reason`/`_qc_per_model` 两键，不该殃及用户同前缀列。
  - 超长列名（8000 字符）：上传/模板渲染/跑数/输出全链路应无碍。
  - 海量列名（1500 列）：上传 + 血缘端点 + 跑数应完成，且列集合完整。
"""
import asyncio
import json

from app.engine import runner
from app.services import llm

USAGE = {"prompt_tokens": 1, "completion_tokens": 1}


async def _upload_jsonl(auth_client, rows, name="刁钻列.jsonl"):
    jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    files = [("files", (name, jsonl, "application/octet-stream"))]
    return (await auth_client.post("/api/datasets/upload", files=files)).json()[0]


async def _make_model(auth_client, name="m1", key="k1"):
    return (await auth_client.post("/api/models", json={
        "name": name, "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": key, "default_params": {}})).json()


def _linear_graph(ds_id, mc_id, user_prompt="Q:{{q}}", output_column="a"):
    """input -> llm_synth -> output 线性图（无质检，仅压列处理本身）。"""
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "user_prompt": user_prompt,
                "output_column": output_column, "concurrency": 4, "retries": 1}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "out", "kind": "normal"},
        ],
    }


async def _echo_chat(mc, system, user, params=None, retries=3):
    return f"答:{user}", USAGE


async def _run_to_done(auth_client, wf_id):
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    run = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        run = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if run["status"] in ("completed", "failed", "cancelled"):
            break
    return run_id, run


async def test_user_column_with_qc_prefix_must_survive(auth_client, monkeypatch, session_factory):
    """用户数据里以 _qc 开头的列（如 _qc_score）必须原样保留到输出，
    不能被内部保留前缀 startswith("_qc") 静默吃掉（只该剔除运行期注入的两键）。"""
    ds = await _upload_jsonl(auth_client, [
        {"q": "问题一", "_qc_score": "用户自带评分-1"},
        {"q": "问题二", "_qc_score": "用户自带评分-2"},
    ])
    assert set(ds["columns"]) == {"q", "_qc_score"}  # 上传阶段尚在
    mc = await _make_model(auth_client)
    graph = _linear_graph(ds["id"], mc["id"])
    wf = (await auth_client.post("/api/workflows", json={"name": "保留前缀"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    monkeypatch.setattr(llm, "chat", _echo_chat)

    run_id, run = await _run_to_done(auth_client, wf["id"])
    assert run["status"] == "completed", run

    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    scores = {r.get("_qc_score") for r in out_rows}
    assert scores == {"用户自带评分-1", "用户自带评分-2"}, \
        f"用户 _qc 前缀列被内部簿记前缀静默吃掉；实际输出行：{out_rows}"


async def test_super_long_column_name_end_to_end(auth_client, monkeypatch, session_factory):
    """超长列名（8000 字符）：上传/模板渲染/跑数/输出全链路不应崩。"""
    long_col = "列" * 4000  # 4000 字符（UTF-8 约 1.2w 字节）
    ds = await _upload_jsonl(auth_client, [{long_col: "超长列的值", "q": "x"}])
    assert long_col in ds["columns"]
    mc = await _make_model(auth_client)
    graph = _linear_graph(ds["id"], mc["id"], user_prompt="取值:{{" + long_col + "}}")
    wf = (await auth_client.post("/api/workflows", json={"name": "超长列名"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    monkeypatch.setattr(llm, "chat", _echo_chat)

    run_id, run = await _run_to_done(auth_client, wf["id"])
    assert run["status"] == "completed", run

    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert out_rows[0]["a"] == "答:取值:超长列的值"  # 模板成功取到超长列的值
    assert out_rows[0][long_col] == "超长列的值"      # 超长列名原样保留


async def test_many_columns_upload_lineage_and_run(auth_client, monkeypatch, session_factory):
    """海量列名（1500 列）：上传/血缘端点/跑数应完成，列集合完整。"""
    n = 1500
    row = {f"col{i:04d}": f"v{i}" for i in range(n)}
    ds = await _upload_jsonl(auth_client, [row])
    assert len(ds["columns"]) == n
    mc = await _make_model(auth_client)
    graph = _linear_graph(ds["id"], mc["id"], user_prompt="{{col0000}}", output_column="ans")
    wf = (await auth_client.post("/api/workflows", json={"name": "海量列"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    cols = (await auth_client.get(f"/api/workflows/{wf['id']}/columns")).json()
    assert "ans" in cols["gen"]["output"]
    assert len(cols["gen"]["output"]) == n + 1  # 输入 n 列 + 新增 ans

    monkeypatch.setattr(llm, "chat", _echo_chat)
    run_id, run = await _run_to_done(auth_client, wf["id"])
    assert run["status"] == "completed", run

    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows[0]) == n + 1       # 1500 原列 + ans
    assert out_rows[0]["ans"] == "答:v0"   # 模板取 col0000=v0
