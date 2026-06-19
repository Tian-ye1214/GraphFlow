"""复杂 Excel × 真实模型：解析 samples/complex_test.xlsx（多 sheet，真解析保真）→ 用库里配置的
DeepSeek 真跑两条复杂链路，暴露 mock 测不到的真实模型行为 / 数据边角 bug。绝不输出任何密钥材料。

链路1（列CRUD + 真实双合成菱形合并）：input→clean(rename/cast/concat/dedup/sample)→genA/genB(真合成)→out(合并)
链路2（真实合成 + 真实质检 + 回扫）：input→clean(filter/dedup/sample)→gen(真合成)→qc(真判定K-of-N,回扫)→out

LLM 阶段对行数封顶（CAP / CAP2），避免数百次真实调用；解析/列CRUD 仍走完整 200 行。
产物留在库里（LIVE-* 前缀），可在 UI 查看 / 删除。

用法（backend 目录，需真实 DeepSeek 配置）：
    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/complex_excel_live.py [model_config_id]
"""
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app import db
from app.db import get_session_factory, init_db
from app.engine import runner
from app.engine.manager import manager
from app.models import ModelConfig, Run, Workflow
from app.routers.datasets import create_dataset
from app.services import run_service
from app.services.file_parse import parse_sheets

XLSX = Path(__file__).resolve().parents[2] / "samples" / "complex_test.xlsx"
CAP = 10    # 链路1 每个合成节点真实调用行数上限
CAP2 = 6    # 链路2 合成 + 质检行数上限
CHECKS = []


def chk(name, ok, detail=""):
    CHECKS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


async def _mk_wf(s, user_id, name, graph):
    wf = Workflow(user_id=user_id, name=name, graph_json=json.dumps(graph, ensure_ascii=False))
    s.add(wf)
    await s.commit()
    return wf.id


async def _run(sf, user_id, wf_id, timeout):
    rid = await run_service.enqueue_run(sf, user_id, wf_id)
    try:
        await asyncio.wait_for(manager.wait(rid), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  [超时 {timeout}s] 取消 run {rid}")
        manager.cancel(rid)
        await manager.wait(rid)
    async with sf() as s:
        run = await s.get(Run, rid)
    return rid, run


def _chain1_graph(ds_id, mc_id):
    """列CRUD（rename/cast/concat/dedup/sample 封顶）→ 真实双合成（summary/keywords）→ 菱形合并。"""
    return {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "clean", "type": "auto_process", "config": {"seed": 7, "operations": [
            {"op": "rename", "mapping": {"question": "q", "answer": "a"}},
            {"op": "cast", "column": "num_clean", "to": "int"},
            {"op": "concat", "target": "q_cat", "columns": ["q", "category"], "sep": " | "},
            {"op": "dedup", "columns": ["q"]},
            {"op": "sample", "n": CAP},
        ]}},
        {"id": "genA", "type": "llm_synth", "config": {
            "model_config_id": mc_id, "concurrency": 4, "retries": 3,
            "user_prompt": "用一句话（不超过30字）概括这个问题考察的核心知识点：{{q}}",
            "output_column": "summary"}},
        {"id": "genB", "type": "llm_synth", "config": {
            "model_config_id": mc_id, "concurrency": 4, "retries": 3,
            "user_prompt": "为下面的问题给出3个中文关键词，用逗号分隔，只输出关键词本身：{{q}}",
            "output_column": "keywords"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "clean", "kind": "normal"},
        {"source": "clean", "target": "genA", "kind": "normal"},
        {"source": "clean", "target": "genB", "kind": "normal"},
        {"source": "genA", "target": "out", "kind": "normal"},
        {"source": "genB", "target": "out", "kind": "normal"}],
    }


def _chain2_graph(ds_id, mc_id):
    """真实合成（刻意限长，逼出部分不过检）→ 真实质检 → 回扫重生一轮。"""
    judge_sys = "你是严格质检员：答案必须【正确回答了问题】且【不超过15个汉字】才算通过，超长或答错一律不通过。"
    judge_user = '问题：{{q}}\n答案：{{answer}}\n返回 JSON：{"status": "pass" 或 "failed", "reason": "理由"}'
    return {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "clean", "type": "auto_process", "config": {"seed": 7, "operations": [
            {"op": "rename", "mapping": {"question": "q"}},
            {"op": "filter", "column": "category", "mode": "regex", "value": "数学|物理|化学"},
            {"op": "dedup", "columns": ["q"]},
            {"op": "sample", "n": CAP2},
        ]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc_id, "concurrency": 4, "retries": 3,
            "user_prompt": "只用不超过15个汉字、简洁回答：{{q}}", "output_column": "answer"}},
        {"id": "qc", "type": "qc", "config": {
            "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 1, "concurrency": 4,
            "system_prompt": judge_sys, "user_prompt": judge_user, "params": {"temperature": 0}}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "clean", "kind": "normal"},
        {"source": "clean", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"},
        {"source": "qc", "target": "gen", "kind": "rescan"}],
    }


async def main():
    await init_db()
    sf = get_session_factory()
    want = int(sys.argv[1]) if len(sys.argv) > 1 else None
    async with sf() as s:
        mcs = (await s.execute(select(ModelConfig).order_by(ModelConfig.id))).scalars().all()
        mc = (next((m for m in mcs if m.id == want), None) if want
              else next((m for m in mcs if "deepseek" in m.base_url.lower()), None))
        if mc is None:
            print("找不到 DeepSeek 模型配置（或指定 id 不存在）"); return
        user_id, mc_id = mc.user_id, mc.id
    print(f"[模型] id={mc_id} model_name={mc.model_name!r} base_url={mc.base_url!r} user_id={user_id}")

    # ── 1) 解析真文件：多 sheet + 解析保真 ──────────────────────────────────
    print("\n=== 1) 解析 samples Excel（多 sheet + 保真）===")
    parsed = parse_sheets("complex_test.xlsx", XLSX.read_bytes())
    names = [n for n, _ in parsed]
    chk("多 sheet → 主数据/配置/数值表/边界（空表跳过）",
        set(names) == {"complex_test-主数据", "complex_test-配置", "complex_test-数值表", "complex_test-边界"},
        f"names={names}")
    main_rows = dict(parsed)["complex_test-主数据"]
    chk("主数据 行数=200", len(main_rows) == 200, f"rows={len(main_rows)}")
    r0 = main_rows[0]
    chk("前导零 id 保字符串", r0["id"] == "001", f"id={r0['id']!r}")
    chk("20 位长 ID 不丢精度", r0["big_id"] == "12345678901234567890")
    chk("JSON 字符串保字符串(不被解析成对象)", isinstance(r0["json_str"], str), f"type={type(r0['json_str']).__name__}")
    chk("多行单元格内嵌换行保留", "\n" in main_rows[0]["multiline"])
    chk("真实日期 → ISO 字符串", isinstance(r0["ts"], str) and r0["ts"].startswith("2026-01-01"), f"ts={r0['ts']!r}")
    chk("空格填充数字串两侧空格保留", r0["space_pad"].startswith(" ") and r0["space_pad"].endswith(" "))
    longrow = next((rr for rr in main_rows if len(str(rr.get("long_text", ""))) == 32000), None)
    chk("超长值(32000)保留", longrow is not None)

    ds_ids = {}
    async with sf() as s:
        for name, rows in parsed:
            ds = await create_dataset(s, user_id, f"LIVE-{name}", rows, source="upload")
            ds_ids[name] = ds.id
    main_ds = ds_ids["complex_test-主数据"]
    print(f"  已建数据集：{ds_ids}")

    # ── 2) 链路1：列CRUD + 真实双合成菱形合并 ───────────────────────────────
    print(f"\n=== 2) 链路1：列CRUD + 真实双合成菱形合并（每支真跑 ≤{CAP} 行）===")
    async with sf() as s:
        wf1 = await _mk_wf(s, user_id, "LIVE-Excel-双合成菱形", _chain1_graph(main_ds, mc_id))
    rid1, run1 = await _run(sf, user_id, wf1, timeout=300)
    chk("链路1 完成（无崩）", run1.status == "completed", f"status={run1.status} err={run1.error}")
    out1 = await runner._node_outputs(sf, rid1, "out")
    chk("链路1 有产物", len(out1) > 0, f"out 行数={len(out1)}")
    if out1:
        o = out1[0]
        chk("菱形合并：每行同时有 summary+keywords", all("summary" in rr and "keywords" in rr for rr in out1))
        chk("真实合成非空（summary/keywords 都有内容）",
            all(str(rr.get("summary", "")).strip() and str(rr.get("keywords", "")).strip() for rr in out1))
        chk("rename 生效（q 在、question 没）", "q" in o and "question" not in o)
        chk("cast 生效（num_clean 是 int）", isinstance(o.get("num_clean"), int), f"num_clean={o.get('num_clean')!r}")
        chk("concat 生效（q_cat=q | category）", o.get("q_cat") == f"{o['q']} | {o['category']}")
        chk("_qc 用户列穿真实链路存活", "_qc_score" in o and "_qc_note" in o)
        chk("模板字面量不被二次展开（tmpl_literal 原样）", o.get("tmpl_literal") == "见 {{question}}", f"{o.get('tmpl_literal')!r}")
        chk("怪异列名穿链存活（中文/点号/emoji）", "中文列名" in o and "col.with.dots" in o and "emoji😀列" in o)
        for rr in out1[:3]:
            print(f"    Q={rr.get('q')!r}")
            print(f"      summary={str(rr.get('summary'))[:60]!r}")
            print(f"      keywords={str(rr.get('keywords'))[:60]!r}")

    # ── 3) 链路2：真实合成 + 真实质检 + 回扫 ─────────────────────────────────
    print(f"\n=== 3) 链路2：真实合成 + 真实质检 + 回扫（≤{CAP2} 行）===")
    async with sf() as s:
        wf2 = await _mk_wf(s, user_id, "LIVE-Excel-合成质检回扫", _chain2_graph(main_ds, mc_id))
    rid2, run2 = await _run(sf, user_id, wf2, timeout=300)
    chk("链路2 完成（无崩）", run2.status == "completed", f"status={run2.status} err={run2.error}")
    rate = await run_service.first_round_rate(sf, rid2)
    fails = await run_service.sample_failures(sf, rid2, n=20)
    out2 = await runner._node_outputs(sf, rid2, "out")
    chk("链路2 首轮质检指标已计算", rate is not None, f"首轮通过率={rate}")
    chk("链路2 有通过产物 或 失败样本都带原因",
        len(out2) > 0 or all(f["reasons"] for f in fails), f"通过={len(out2)} 失败={len(fails)}")
    print(f"  首轮通过率={rate} 最终通过={len(out2)} 最终失败={len(fails)}")
    gen2 = await runner._node_outputs(sf, rid2, "gen")
    for rr in gen2[:CAP2]:
        print(f"    Q={rr.get('q')!r} A={str(rr.get('answer'))[:40]!r}")
    for f in fails[:3]:
        rs = [pm.get('reason') for pm in f['reasons']] if f['reasons'] else f['reasons']
        print(f"    ✗ {f['sample'].get('q')!r} ans={f['sample'].get('answer')!r} → {rs}")

    # ── 汇总 ────────────────────────────────────────────────────────────────
    n_pass = sum(1 for ok, _ in CHECKS if ok)
    print(f"\n{'=' * 60}\n汇总：{n_pass}/{len(CHECKS)} 通过  | 链路1 run={rid1} 链路2 run={rid2}")
    not_ok = [name for ok, name in CHECKS if not ok]
    if not_ok:
        print("失败检查项：")
        for name in not_ok:
            print(f"  ✗ {name}")
    else:
        print("全部检查通过 ✓")
    print("\n[产物留在库里] LIVE-* 数据集/工作流/运行，可在 UI 查看或删除。")
    await db.engine.dispose()


asyncio.run(main())
