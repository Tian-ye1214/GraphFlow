"""第二轮缺陷探测：非对象上传、菱形/合并图（列血缘 vs 执行）、导出特殊值/大文件。
隔离临时库 + 确定性假模型。用法：PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/bughunt2.py
"""
import asyncio
import io
import json
import tempfile
from pathlib import Path

from app.config import settings

U = {"prompt_tokens": 1, "completion_tokens": 1}


def banner(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def _patch(fn):
    from app.services import llm

    async def fake(mc, system, user, params=None, retries=3):
        return fn(user)

    llm.chat = fake


async def _client(raise_exc=False):
    import httpx
    from app.main import create_app
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=raise_exc),
        base_url="http://test")


# --------------------------------------------------------------- 1) 非对象上传
async def probe_non_object_upload():
    banner("1) 上传「非对象」JSON/JSONL（标量/null/标量数组）应给 422，而非 500")
    c = await _client(raise_exc=False)
    await c.post("/api/auth/login", json={"username": "u1"})

    async def up(name, raw):
        files = [("files", (name, io.BytesIO(raw), "application/octet-stream"))]
        r = await c.post("/api/datasets/upload", files=files)
        tag = "OK" if r.status_code == 422 else f"!! {r.status_code}"
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:80]
        print(f"  {name:18} -> {r.status_code} [{tag}]  {str(body)[:90]}")

    await up("scalar.json", b"42")
    await up("null.json", b"null")
    await up("arr.json", b"[1, 2, 3]")
    await up("bare.jsonl", '{"q": 1}\n99\n'.encode())
    await up("str.json", b'"hello"')
    print("  期望：全部 422（带可读报错）。现状若出现 500 即 BUG（union_columns 对非 dict 行抛 TypeError 未捕获）")
    await c.aclose()


# --------------------------------------------------------------- 2) 菱形合并图
async def probe_diamond_merge():
    banner("2) 菱形/合并图：两上游汇入一个节点——执行(行)与列血缘(列)是否自洽")
    from app.engine.manager import manager
    _patch(lambda u: (f"A:{u}", U) if u.startswith("A:") or "支A" in u else (f"B:{u}", U))
    c = await _client(raise_exc=False)
    await c.post("/api/auth/login", json={"username": "u2"})
    body = '{"q": "问0"}\n{"q": "问1"}\n{"q": "问2"}\n'.encode()
    ds = (await c.post("/api/datasets/upload",
          files=[("files", ("d.jsonl", io.BytesIO(body), "application/octet-stream"))])).json()[0]
    mc = (await c.post("/api/models", json={"name": "m", "model_name": "f",
          "base_url": "http://x/v1", "api_key": "k", "default_params": {}})).json()
    wf = (await c.post("/api/workflows", json={"name": "菱形"})).json()
    # in -> A(加列 a) ; in -> B(加列 b) ; A,B -> out
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
        {"id": "A", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "支A:{{q}}", "output_column": "a"}},
        {"id": "B", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "支B:{{q}}", "output_column": "b"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "A", "kind": "normal"},
        {"source": "in", "target": "B", "kind": "normal"},
        {"source": "A", "target": "out", "kind": "normal"},
        {"source": "B", "target": "out", "kind": "normal"}]}
    await c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    cols = (await c.get(f"/api/workflows/{wf['id']}/columns")).json()
    rid = (await c.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await manager.wait(rid)
    rows = (await c.get(f"/api/runs/{rid}/rows?node_id=out&page_size=100")).json()["rows"]
    print(f"  输入 3 行，两分支各产 3 行。out 实际行数 = {len(rows)}")
    print(f"  列血缘说 out.input = {cols['out']['input']}")
    have_both = [r for r in rows if "a" in r and "b" in r]
    have_a = [r for r in rows if "a" in r and "b" not in r]
    have_b = [r for r in rows if "b" in r and "a" not in r]
    print(f"  实际行里：同时有 a&b 的 {len(have_both)} 行；只有 a 的 {len(have_a)} 行；只有 b 的 {len(have_b)} 行")
    print(f"  样例行：{rows[0] if rows else None}")
    print("  期望：列血缘应如实反映「没有任何一行同时拥有 a 和 b」；现状 union 列谎称 [q,a,b] 齐备 → 误导建链")
    await c.aclose()


# ------------------------------------------------------------- 3) 导出特殊值
async def probe_export_special():
    banner("3) 导出特殊值 / 大文件：嵌套/None/混合类型/公式串/超长，三格式是否崩或损")
    from app.services.export import export_rows
    rows = [
        {"q": "正常", "n": 1},
        {"q": "嵌套dict", "n": {"x": 1, "y": [2, 3]}},
        {"q": "列表", "n": [1, 2, 3]},
        {"q": "缺失", "n": None},
        {"q": "公式注入", "n": "=1+1+cmd|' /C calc'!A0"},
        {"q": "超长", "n": "字" * 50000},
        {"extra_only": "异构列"},   # 列集合不一致
    ]
    out = settings.data_dir / "exp"
    for fmt in ("jsonl", "csv", "xlsx"):
        try:
            p = export_rows(rows, fmt, out.with_suffix("." + fmt))
            size = p.stat().st_size
            note = ""
            if fmt == "csv":
                head = p.read_text(encoding="utf-8-sig").splitlines()
                # 找公式注入那一行是否原样以 = 开头（CSV 注入）
                inj = [ln for ln in head if ln.lstrip('"').startswith("=1+1")]
                note = f"; 公式串未转义={'是(可注入)' if inj else '否'}"
            print(f"  {fmt:5} -> OK  {size} 字节{note}")
        except Exception as e:
            print(f"  {fmt:5} -> 崩溃: {type(e).__name__}: {str(e)[:120]}")
    # 大文件
    big = [{"i": i, "text": f"行{i}的内容" * 20} for i in range(5000)]
    import time
    for fmt in ("jsonl", "csv", "xlsx"):
        t0 = time.monotonic()
        try:
            p = export_rows(big, fmt, out.with_name("big").with_suffix("." + fmt))
            print(f"  big({len(big)}) {fmt:5} -> {p.stat().st_size} 字节 {time.monotonic()-t0:.2f}s")
        except Exception as e:
            print(f"  big {fmt:5} -> 崩溃: {type(e).__name__}: {str(e)[:120]}")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="gf_bughunt2_"))
    settings.data_dir = tmp
    print(f"[隔离临时库] {settings.db_url}")
    from app import db
    await db.init_db()
    for fn in (probe_non_object_upload, probe_diamond_merge, probe_export_special):
        try:
            await fn()
        except Exception as e:
            import traceback
            print(f"[场景异常] {fn.__name__}: {e}")
            traceback.print_exc()
    await db.engine.dispose()
    print(f"\n[完成] {tmp}")


asyncio.run(main())
