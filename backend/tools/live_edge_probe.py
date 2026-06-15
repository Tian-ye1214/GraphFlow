"""真实模型边角探针：刻意喂「空/纯空白/单遍渲染/emoji」题面给真实 DeepSeek，验证 mock 测不到的行为：
  1) 空/空白提示词送真实 API 时，单行失败不拖垮整 run（逐行隔离，run 仍 completed）；
  2) render_template 单遍渲染——题面里的 {{x}} 作为「数据」不被二次展开；
  3) 真实质检对「内容为空」样本的处理。
绝不输出任何密钥材料。产物留库（LIVE-EDGE-* 前缀）。

用法（backend 目录，需真实 DeepSeek）：
    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/live_edge_probe.py [model_config_id]
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app import db
from app.db import get_session_factory, init_db
from app.engine import runner
from app.engine.manager import manager
from app.models import ModelConfig, Run, RunRow, Workflow
from app.routers.datasets import create_dataset
from app.services import run_service

# 刻意刁钻的题面：空、纯空白、单遍渲染探针、emoji、正常对照
ROWS = [
    {"q": ""},                       # 空：渲染成空提示词 → 真实 API 多半报错 → 该行失败但不崩 run
    {"q": "   "},                    # 纯空白
    {"q": "请原样重复这串：{{leak}}"},   # {{leak}} 是数据的一部分，不应被二次展开成别的列
    {"q": "用一个 emoji 回答：你开心吗？😀"},
    {"q": "标准大气压下水的沸点是多少摄氏度？"},  # 正常对照
]


def _graph(ds_id, mc_id):
    judge_sys = "你是质检员：答案是否切题、非空、合理地回应了问题？返回 JSON。"
    judge_user = '问题：{{q}}\n答案：{{answer}}\n返回 JSON：{"pass": true/false, "reason": "理由"}'
    return {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc_id, "concurrency": 4, "retries": 2,
            "user_prompt": "{{q}}", "output_column": "answer"}},  # 直接把题面当提示词，逼出空/单遍渲染边角
        {"id": "qc", "type": "qc", "config": {
            "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 1, "concurrency": 4,
            "system_prompt": judge_sys, "user_prompt": judge_user, "params": {"temperature": 0}}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}],
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
            print("找不到 DeepSeek 模型配置"); return
        user_id, mc_id = mc.user_id, mc.id
    print(f"[模型] id={mc_id} model_name={mc.model_name!r} base_url={mc.base_url!r} user_id={user_id}")

    async with sf() as s:
        ds = await create_dataset(s, user_id, "LIVE-EDGE-刁钻题面", ROWS, source="upload")
        wf = Workflow(user_id=user_id, name="LIVE-EDGE-探针",
                      graph_json=json.dumps(_graph(ds.id, mc_id), ensure_ascii=False))
        s.add(wf); await s.commit()
        ds_id, wf_id = ds.id, wf.id
    print(f"[准备] 数据集={ds_id} 工作流={wf_id}（{len(ROWS)} 行）")

    rid = await run_service.enqueue_run(sf, user_id, wf_id)
    try:
        await asyncio.wait_for(manager.wait(rid), timeout=240)
    except asyncio.TimeoutError:
        print("[超时] 取消"); manager.cancel(rid); await manager.wait(rid)
    async with sf() as s:
        run = await s.get(Run, rid)
        gen_recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == rid, RunRow.node_id == "gen").order_by(RunRow.row_idx))).scalars().all()

    print(f"\n[终态] status={run.status!r}（逐行隔离：即便空行失败，整 run 也应 completed）")
    g_done = [r for r in gen_recs if r.status == "done"]
    g_failed = [r for r in gen_recs if r.status == "failed"]
    print(f"[gen] done={len(g_done)} failed={len(g_failed)}")
    for r in gen_recs:
        if r.status == "done":
            ans = json.loads(r.data_json)[0].get("answer", "")
            print(f"  ✓ row{r.row_idx} A={str(ans)[:50]!r}")
        else:
            print(f"  ✗ row{r.row_idx} err={str(r.error)[:80]!r}")

    # 单遍渲染：第 2 行(idx=2) 题面含 {{leak}}，gen 的输入行不应凭空冒出 leak 列
    gen_out = await runner._node_outputs(sf, rid, "gen")
    leaked = any("leak" in row for row in gen_out)
    print(f"\n[单遍渲染] gen 产物里是否凭空出现 'leak' 列：{leaked}（应为 False）")

    rate = await run_service.first_round_rate(sf, rid)
    out_rows = await runner._node_outputs(sf, rid, "out")
    fails = await run_service.sample_failures(sf, rid, n=20)
    print(f"[质检] 首轮通过率={rate} 通过={len(out_rows)} 失败={len(fails)}")
    for f in fails[:5]:
        rs = [pm.get('reason') for pm in f['reasons']] if f['reasons'] else f['reasons']
        print(f"  ✗ q={f['sample'].get('q')!r} ans={str(f['sample'].get('answer'))[:40]!r} → {rs}")

    ok = (run.status == "completed") and (not leaked)
    print(f"\n{'=' * 50}\n[结论] run 完成且逐行隔离={run.status == 'completed'}；单遍渲染无泄漏={not leaked} → {'PASS' if ok else 'FAIL'}")
    print("[产物留库] LIVE-EDGE-* 可在 UI 查看 / 删除。")
    await db.engine.dispose()


asyncio.run(main())
