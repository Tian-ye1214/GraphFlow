"""刁钻数据真实端到端测试：列名重复 / 编码（BOM·GBK）/ 缺失值 / 去重 / 诡异模板。

真实链路：/api/datasets/upload（含编码解析）→ /api/workflows → /api/runs（后台真实跑）。
延续第八/九批「真实测试暴露 RED → KISS 最小修复转绿」的做法，暴露 bug 为主。

预期暴露的真实缺陷：
  - 缺失值：CSV 空单元格 → NaN → null → None，render_template 渲染成字面量 "None" 进提示词。
  - 编码：UTF-8 BOM 污染首列名（﻿ 前缀）；GBK（Windows 中文 Excel 默认）整文件上传失败。
  - 去重：_dedup 用 str(None) 键，None 与字面量字符串 "None" 撞键 → 静默丢数据。
诚实记录的非 bug：CSV 重复列名（pandas 改名 a/a.1，数据无损）、诡异模板（缺列→空、点号列名可取）。
"""
import asyncio
import json

from app.engine import runner
from app.services import llm

USAGE = {"prompt_tokens": 1, "completion_tokens": 1}


async def _upload(auth_client, content: bytes, name: str):
    """原始字节上传，返回 httpx 响应（便于断言 200/422 与编码解析结果）。"""
    files = [("files", (name, content, "application/octet-stream"))]
    return await auth_client.post("/api/datasets/upload", files=files)


async def _upload_jsonl(auth_client, rows, name="数据.jsonl"):
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    return (await _upload(auth_client, body, name)).json()[0]


async def _make_model(auth_client, name="m1", key="k1"):
    return (await auth_client.post("/api/models", json={
        "name": name, "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": key, "default_params": {}})).json()


def _linear_graph(ds_id, mc_id, user_prompt="Q:{{q}}", output_column="a"):
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


def _dedup_graph(ds_id, columns):
    """input -> auto_process(dedup by columns) -> output，纯算子链，无需模型。"""
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "proc", "type": "auto_process", "config": {
                "operations": [{"op": "dedup", "columns": columns}]}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "proc", "kind": "normal"},
            {"source": "proc", "target": "out", "kind": "normal"},
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


async def _build_run(auth_client, monkeypatch, session_factory, ds_id, graph, name):
    wf = (await auth_client.post("/api/workflows", json={"name": name})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    monkeypatch.setattr(llm, "chat", _echo_chat)
    run_id, run = await _run_to_done(auth_client, wf["id"])
    assert run["status"] == "completed", run
    return await runner._node_outputs(session_factory, run_id, "out")


# ── 缺失值 ────────────────────────────────────────────────────────────────────
async def test_csv_missing_value_renders_empty_not_none(auth_client, monkeypatch, session_factory):
    """CSV 空单元格 → None；模板 {{note}} 必须渲染成空串，而不是字面量 "None" 进提示词。"""
    csv = "q,note\n甲,\n乙,有备注\n".encode("utf-8")  # 第一行 note 为空
    ds = (await _upload(auth_client, csv, "缺失.csv")).json()[0]
    mc = await _make_model(auth_client)
    graph = _linear_graph(ds["id"], mc["id"], user_prompt="备注:{{note}}")
    out = await _build_run(auth_client, monkeypatch, session_factory, ds["id"], graph, "缺失值")

    by_q = {r["q"]: r["a"] for r in out}
    assert by_q["甲"] == "答:备注:", f"缺失值被渲染成字面量 None：{by_q['甲']!r}"
    assert by_q["乙"] == "答:备注:有备注"


# ── 编码 ──────────────────────────────────────────────────────────────────────
async def test_csv_utf8_bom_first_column_not_polluted(auth_client, monkeypatch, session_factory):
    """带 UTF-8 BOM 的 CSV：首列名不应被 \\ufeff 污染，模板 {{q}} 应能正常取值。"""
    csv = ("﻿" + "q,v\n问,1\n").encode("utf-8")  # 前缀 BOM
    ds = (await _upload(auth_client, csv, "bom.csv")).json()[0]
    assert ds["columns"] == ["q", "v"], f"BOM 污染了列名：{ds['columns']}"

    mc = await _make_model(auth_client)
    graph = _linear_graph(ds["id"], mc["id"], user_prompt="取:{{q}}")
    out = await _build_run(auth_client, monkeypatch, session_factory, ds["id"], graph, "BOM")
    assert out[0]["a"] == "答:取:问"


async def test_jsonl_utf8_bom_parses(auth_client):
    """带 BOM 的 .jsonl：应能解析而非 JSONDecodeError。"""
    body = ("﻿" + json.dumps({"q": "你好"}, ensure_ascii=False) + "\n").encode("utf-8")
    r = await _upload(auth_client, body, "bom.jsonl")
    assert r.status_code == 200, r.text
    assert r.json()[0]["columns"] == ["q"]


async def test_gbk_csv_uploads(auth_client):
    """GBK 编码 CSV（Windows 中文 Excel/记事本常见）：应能解析，而非整文件上传失败。"""
    csv = "问题,答案\n你好,世界\n".encode("gbk")
    r = await _upload(auth_client, csv, "gbk.csv")
    assert r.status_code == 200, r.text
    ds = r.json()[0]
    assert ds["columns"] == ["问题", "答案"]
    assert ds["row_count"] == 1


# ── 去重（按 session）──────────────────────────────────────────────────────────
async def test_dedup_by_session_keeps_first_per_session(auth_client, monkeypatch, session_factory):
    """按 session 去重：每个 session 保留首次出现，顺序稳定。"""
    rows = [
        {"session": "s1", "q": "a"},
        {"session": "s1", "q": "b"},   # 与上同 session → 丢
        {"session": "s2", "q": "c"},
        {"session": "s2", "q": "d"},   # 同 s2 → 丢
        {"session": "s3", "q": "e"},
    ]
    ds = await _upload_jsonl(auth_client, rows)
    graph = _dedup_graph(ds["id"], ["session"])
    out = await _build_run(auth_client, monkeypatch, session_factory, ds["id"], graph, "去重")
    assert [r["q"] for r in out] == ["a", "c", "e"]


async def test_dedup_none_session_not_collide_with_literal_none(auth_client, monkeypatch, session_factory):
    """null 的 session 与字面量字符串 "None" 的 session 是不同值，不该撞键被静默去掉。"""
    rows = [
        {"session": None, "q": "null的"},
        {"session": "None", "q": "字面None"},  # 真值是字符串 "None"，与上面 null 不同
    ]
    ds = await _upload_jsonl(auth_client, rows)
    graph = _dedup_graph(ds["id"], ["session"])
    out = await _build_run(auth_client, monkeypatch, session_factory, ds["id"], graph, "去重None")
    qs = {r["q"] for r in out}
    assert qs == {"null的", "字面None"}, f"None 与字符串 'None' 撞键丢数据：{qs}"


# ── 重复列名 / 诡异模板（诚实记录，非 bug 则作回归护栏）──────────────────────────
async def test_csv_duplicate_columns_disambiguated_no_data_loss(auth_client):
    """CSV 重复表头 a,a：pandas 自动消歧为 a / a.1，两列数据都在（非 bug，作护栏）。
    值按 dtype=str 保真为字符串（不再静默推断为数字）。"""
    csv = "a,a\n1,2\n".encode("utf-8")
    ds = (await _upload(auth_client, csv, "dup.csv")).json()[0]
    assert ds["columns"] == ["a", "a.1"]
    rows = (await auth_client.get(f"/api/datasets/{ds['id']}/rows")).json()["rows"]
    assert rows[0] == {"a": "1", "a.1": "2"}


async def test_weird_templates_dotted_missing_and_no_reexpand(auth_client, monkeypatch, session_factory):
    """诡异模板：点号列名可取、缺失列渲染空、值里含 {{...}} 不二次展开、emoji/unicode 列名正常。"""
    rows = [{"a.1": "点号值", "🚀": "火箭", "weird": "里面有{{a.1}}"}]
    ds = await _upload_jsonl(auth_client, rows)
    mc = await _make_model(auth_client)
    # 取点号列 + emoji 列 + 缺失列(缺) + 含占位符的值(不应二次展开)
    prompt = "p1={{a.1}}|p2={{🚀}}|p3={{missing}}|p4={{weird}}"
    graph = _linear_graph(ds["id"], mc["id"], user_prompt=prompt)
    out = await _build_run(auth_client, monkeypatch, session_factory, ds["id"], graph, "诡异模板")
    assert out[0]["a"] == "答:p1=点号值|p2=火箭|p3=|p4=里面有{{a.1}}"
