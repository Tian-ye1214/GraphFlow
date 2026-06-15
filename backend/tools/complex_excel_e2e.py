"""复杂 Excel 端到端：上传 tools/complex_test.xlsx → 跑「列 CRUD + 菱形合并」复杂链路 → 逐项校验历轮修复在组合场景下仍成立。
隔离临时库 + 确定性假模型（列操作/数据边角全是确定性的，无需真实模型）。
用法：PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/complex_excel_e2e.py
"""
import asyncio
import json
import tempfile
from pathlib import Path

from app.config import settings

XLSX = Path(__file__).resolve().parents[2] / "samples" / "complex_test.xlsx"
NAME200 = "超长列名" * 50
U = {"prompt_tokens": 1, "completion_tokens": 1}
CHECKS = []


def chk(name, ok, detail=""):
    CHECKS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def _patch():
    from app.services import llm

    async def fake(mc, system, user, params=None, retries=3):
        if user.startswith("SUM:"):
            return f"摘要[{user[4:]}]", U
        if user.startswith("KW:"):
            return user, U                # 原样回显，用于验「模板不二次展开」
        return user, U

    llm.chat = fake


async def main():
    settings.data_dir = Path(tempfile.mkdtemp(prefix="gf_xlsx_e2e_"))
    from app import db, events
    events.subscribers.clear()
    await db.init_db()
    _patch()
    import httpx
    from app.engine.manager import manager
    from app.main import create_app
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test", timeout=60)
    await c.post("/api/auth/login", json={"username": "xl"})

    # ── 1) 上传多 sheet Excel：每 sheet 一个数据集 + 解析保真 ───────────────
    print("\n=== 1) 上传 + 多 sheet + 解析保真 ===")
    content = XLSX.read_bytes()
    r = await c.post("/api/datasets/upload",
                     files=[("files", ("complex_test.xlsx", content, "application/octet-stream"))])
    chk("上传返回 200", r.status_code == 200, f"status={r.status_code}")
    dsets = r.json()
    names = {d["name"] for d in dsets}
    chk("多 sheet → 多数据集（主数据/配置/数值表/边界，空表跳过）",
        names == {"complex_test-主数据", "complex_test-配置", "complex_test-数值表", "complex_test-边界"},
        f"names={sorted(names)}")
    ds = next(d for d in dsets if d["name"].endswith("主数据"))
    cfg = next((d for d in dsets if d["name"].endswith("配置")), None)
    num = next((d for d in dsets if d["name"].endswith("数值表")), None)
    # 配置 sheet：不同 schema，独立成集
    if cfg:
        chk("配置 sheet 独立成集且列正确", cfg["columns"] == ["category", "weight", "enabled", "note"],
            f"cols={cfg['columns']}")
    # 数值表 sheet：真数值仍是数值（dtype=object 不把数字变字符串）
    if num:
        nrows = (await c.get(f"/api/datasets/{num['id']}/rows?page_size=50")).json()["rows"]
        chk("数值表 真数值保数值（seq=int / ratio=float，不被 dtype 强转成字符串）",
            isinstance(nrows[1]["seq"], int) and isinstance(nrows[1]["ratio"], float),
            f"seq={nrows[1]['seq']!r} ratio={nrows[1]['ratio']!r}")

    cols = ds["columns"]
    chk("主数据 行数=200", ds["row_count"] == 200, f"row_count={ds['row_count']}")
    chk("重复列名消歧 dup/dup.1 都在", "dup" in cols and "dup.1" in cols)
    chk("_qc 前缀用户列存活", "_qc_score" in cols and "_qc_note" in cols)
    chk("中文/点号/emoji/超长 列名都在",
        "中文列名" in cols and "col.with.dots" in cols and "emoji😀列" in cols and NAME200 in cols)

    rows = (await c.get(f"/api/datasets/{ds['id']}/rows?page=1&page_size=200")).json()["rows"]
    r0 = rows[0]
    chk("前导零 id 保字符串", r0["id"] == "001", f"id={r0['id']!r}")
    chk("前导零 phone 保字符串", isinstance(r0["phone"], str) and r0["phone"].startswith("0"), f"phone={r0['phone']!r}")
    chk("20 位长 ID 不丢精度", r0["big_id"] == "12345678901234567890", f"big_id={r0['big_id']!r}")
    chk("布尔字面量保字符串", r0["is_active"] in ("true", "false"), f"is_active={r0['is_active']!r}")
    chk("整列空 → 空串", all(rr["empty_all"] == "" for rr in rows))
    chk("dup/dup.1 数据未丢", r0["dup"] == "A0" and r0["dup.1"] == "B0", f"dup={r0['dup']!r} dup.1={r0['dup.1']!r}")
    longrow = rows[10]
    chk("超长值(32000)保留", isinstance(longrow["long_text"], str) and len(longrow["long_text"]) == 32000,
        f"len={len(longrow['long_text']) if isinstance(longrow['long_text'], str) else 'N/A'}")
    none_vals = [rr["none_or_str"] for rr in rows]
    chk("字面量'None'不被当缺失吞掉(且与空串可辨)", ("None" in none_vals) and ("" in none_vals),
        f"含 'None'={'None' in none_vals} 含 空串={'' in none_vals}")
    # 本轮新增列的解析保真
    chk("多行单元格内嵌换行/制表保留", "\n" in r0["multiline"] and "\t" in r0["multiline"], f"multiline={r0['multiline']!r}")
    chk("真实日期 → ISO 字符串", isinstance(r0["ts"], str) and r0["ts"].startswith("2026-01-01"), f"ts={r0['ts']!r}")
    chk("JSON 字符串保字符串(不被解析成对象)", isinstance(r0["json_str"], str), f"type={type(r0['json_str']).__name__}")
    chk("空格填充数字串两侧空格保留", r0["space_pad"].startswith(" ") and r0["space_pad"].endswith(" "), f"space_pad={r0['space_pad']!r}")
    chk("真实大整数保数值", isinstance(r0["big_num"], int), f"big_num={r0['big_num']!r}")

    # ── 2) 复杂链路：列 CRUD（rename/drop/cast/concat/filter/dedup）+ 菱形双合成合并 ──
    print("\n=== 2) 构建复杂链路 ===")
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
        {"id": "clean", "type": "auto_process", "config": {"operations": [
            {"op": "rename", "mapping": {"question": "q", "answer": "a"}},
            {"op": "drop", "columns": ["empty_all", "mojibake", NAME200, "dup", "dup.1"]},
            {"op": "cast", "column": "num_clean", "to": "int"},
            {"op": "concat", "target": "q_cat", "columns": ["q", "category"], "sep": " | "},
            {"op": "filter", "column": "category", "mode": "regex", "value": "数学|物理"},
            {"op": "dedup", "columns": ["q"]},
        ]}},
        {"id": "genA", "type": "llm_synth", "config": {
            "model_config_id": 0, "user_prompt": "SUM:{{q}}", "output_column": "summary"}},
        {"id": "genB", "type": "llm_synth", "config": {
            "model_config_id": 0, "user_prompt": "KW:{{tmpl_literal}}", "output_column": "keywords"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "clean", "kind": "normal"},
        {"source": "clean", "target": "genA", "kind": "normal"},
        {"source": "clean", "target": "genB", "kind": "normal"},
        {"source": "genA", "target": "out", "kind": "normal"},
        {"source": "genB", "target": "out", "kind": "normal"}],
    }
    mc = (await c.post("/api/models", json={"name": "m", "model_name": "f",
          "base_url": "http://x/v1", "api_key": "k", "default_params": {}})).json()
    for n in graph["nodes"]:
        if n["type"] == "llm_synth":
            n["config"]["model_config_id"] = mc["id"]
    wf = (await c.post("/api/workflows", json={"name": "复杂Excel链路"})).json()
    await c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    colr = await c.get(f"/api/workflows/{wf['id']}/columns")
    chk("列血缘 /columns 返 200（不 500）", colr.status_code == 200, f"status={colr.status_code}")
    if colr.status_code == 200:
        lin = colr.json()
        chk("血缘：out 含 summary+keywords（合并节点 union 正确）",
            "summary" in lin["out"]["input"] and "keywords" in lin["out"]["input"],
            f"out.input={lin['out']['input']}")

    # ── 3) 跑数 ───────────────────────────────────────────────────────────
    print("\n=== 3) 跑数 + 产物校验 ===")
    rid = (await c.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    await manager.wait(rid)
    detail = (await c.get(f"/api/runs/{rid}")).json()
    chk("整 run 完成（无崩）", detail["status"] == "completed", f"status={detail['status']} err={detail.get('error')}")
    out = (await c.get(f"/api/runs/{rid}/rows?node_id=out&page_size=200")).json()["rows"]
    chk("有产物行", len(out) > 0, f"out 行数={len(out)}")
    if out:
        o0 = out[0]
        ocols = set(o0)
        chk("rename 生效（q/a 在、question/answer 没）", "q" in ocols and "question" not in ocols)
        chk("drop 生效（empty_all/mojibake/dup 都没）",
            not ({"empty_all", "mojibake", NAME200, "dup", "dup.1"} & ocols))
        chk("cast 生效（num_clean 变 int）", isinstance(o0.get("num_clean"), int), f"num_clean={o0.get('num_clean')!r}")
        chk("concat 生效（q_cat = q | category）", o0.get("q_cat") == f"{o0['q']} | {o0['category']}",
            f"q_cat={o0.get('q_cat')!r}")
        chk("filter 生效（只剩 数学/物理）", all(rr["category"] in ("数学", "物理") for rr in out))
        chk("dedup 生效（q 唯一且行数<40）",
            len({rr["q"] for rr in out}) == len(out) and len(out) < 40, f"out 行数={len(out)}")
        chk("_qc 用户列穿过全链路存活", "_qc_score" in ocols and "_qc_note" in ocols)
        chk("菱形合并：每行同时有 summary 和 keywords",
            all("summary" in rr and "keywords" in rr for rr in out))
        chk("synth 产出正确（summary=摘要[q]）", o0["summary"] == f"摘要[{o0['q']}]", f"summary={o0['summary']!r}")
        chk("模板不二次展开（keywords 保留字面 {{question}}）", "{{question}}" in o0["keywords"],
            f"keywords={o0['keywords']!r}")

    # ── 4) 导出回环（超长值 + unicode 走 xlsx 导出）─────────────────────────
    print("\n=== 4) 导出回环 ===")
    for fmt in ("jsonl", "csv", "xlsx"):
        e = await c.get(f"/api/runs/{rid}/export?node_id=out&format={fmt}")
        chk(f"导出 {fmt} 成功", e.status_code == 200, f"status={e.status_code} {len(e.content)} 字节")

    await c.aclose()
    await db.engine.dispose()

    n_pass = sum(1 for ok, *_ in CHECKS if ok)
    print(f"\n{'=' * 60}\n汇总：{n_pass}/{len(CHECKS)} 通过")
    fails = [(name, detail) for ok, name, detail in CHECKS if not ok]
    if fails:
        print("失败项：")
        for name, detail in fails:
            print(f"  ✗ {name}  {detail}")
    else:
        print("全部通过 ✓")


asyncio.run(main())
