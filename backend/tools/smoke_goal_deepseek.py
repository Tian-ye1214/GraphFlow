"""目标模式真实链路冒烟：用库里已配置的 DeepSeek 跑「合成 → 质检 → 回扫 → 首轮指标 → 跳出判定」全链。

刻意喂十分奇怪、语义模糊的题目，外加一行缺失值(None)题面，观察真实模型在质检链路下的通过率，
再用 run_service/goal_loop 演示目标循环如何消费真实指标做跳出判定。绝不输出任何密钥材料。

用法（在 backend 目录）：
    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/smoke_goal_deepseek.py [model_config_id]
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app.agent import goal_loop
from app.db import get_session_factory, init_db
from app.engine import runner
from app.engine.manager import manager
from app.models import ModelConfig, Run, Workflow
from app.routers.datasets import create_dataset
from app.services import run_service

GOAL_TEXT = "把首轮质检通过率提到 60% 以上"

# 十分奇怪、语义模糊的题目 + 一行缺失值(None)题面 + 一个正常题作对照
ROWS = [
    {"instruction": "如果蓝色的声音比星期二更重，那么三除以悲伤等于几？请给出确切数字。"},
    {"instruction": "请把这句话翻译成它自己，并解释翻译过程中的语义损耗。"},
    {"instruction": "在完全不使用任何词语的前提下，用一段话详细论证什么是沉默。"},
    {"instruction": "下个月的昨天，加上🦄 含有的卡路里，总共等于多少？"},
    {"instruction": None},  # 缺失值：题面为空，模板应渲染成空串而非字面量 "None"
    {"instruction": "用一句正常的话回答：标准大气压下水的沸点是多少摄氏度？"},
]


def _graph(ds_id: int, mc_id: int) -> dict:
    judge_sys = ("你是极其严格的数据质检员。判断「答案」是否真正、准确、切题地回应了「问题」。"
                 "对荒谬或无意义的问题，只有当答案明确指出其无法回答并给出合理说明时才算通过；"
                 "答案敷衍、答非所问、或强行编造数字一律不通过。")
    judge_user = ('问题：{{instruction}}\n答案：{{answer}}\n'
                  '只返回 JSON：{"status": "pass" 或 "failed", "reason": "一句话理由"}')
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "answer",
                "concurrency": 2, "retries": 2,
                "user_prompt": "认真回答下面的问题；若问题本身荒谬/无意义，请明确说明并解释原因：\n\n{{instruction}}"}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 1, "concurrency": 2,
                "system_prompt": judge_sys, "user_prompt": judge_user, "params": {"temperature": 0}}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
            {"source": "qc", "target": "out", "kind": "normal"},
            {"source": "qc", "target": "gen", "kind": "rescan"},  # 失败样本回扫重生一轮
        ],
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
        ds = await create_dataset(s, user_id, "目标模式冒烟-刁钻题", ROWS, source="upload")
        wf = Workflow(user_id=user_id, name="目标模式冒烟", graph_json=json.dumps(_graph(ds.id, mc_id)))
        s.add(wf); await s.commit()
        ds_id, wf_id = ds.id, wf.id
    print(f"[准备] 数据集 id={ds_id} row_count={ds.row_count}；工作流 id={wf_id}")

    run_id = await run_service.enqueue_run(sf, user_id, wf_id)
    print(f"[入队] run_id={run_id}，等待真实 DeepSeek 跑完……")
    try:
        await asyncio.wait_for(manager.wait(run_id), timeout=540)
    except asyncio.TimeoutError:
        print("[超时] 540s 未跑完，提前查看当前状态"); manager.cancel(run_id)

    async with sf() as s:
        run = await s.get(Run, run_id)
    print(f"\n[终态] status={run.status} error={run.error!r}")

    gen_rows = await runner._node_outputs(sf, run_id, "gen")
    print("\n— 合成答案（节选）—")
    for r in gen_rows:
        a = str(r.get("answer", "")).replace("\n", " ")
        print(f"  Q={r.get('instruction')!r}\n    A={a[:140]}{'…' if len(a) > 140 else ''}")

    rate = await run_service.first_round_rate(sf, run_id)
    fails = await run_service.sample_failures(sf, run_id, n=20)
    print(f"\n[首轮质检通过率] {rate}")
    print(f"[失败样本] {len(fails)} 条")
    for f in fails:
        reasons = [pm.get("reason") for pm in f["reasons"]] if f["reasons"] else f["reasons"]
        print(f"  ✗ {f['sample'].get('instruction')!r} → {reasons}")

    thr = run_service.parse_threshold(GOAL_TEXT)
    d = goal_loop.decide(metric=rate, threshold=thr, best=0.0, no_improve=0, no_improve_k=2)
    print(f"\n[目标] {GOAL_TEXT!r} → 解析阈值 {thr}")
    print(f"[跳出判定] stop={d.stop} success={d.success} reason={d.reason!r}")
    print(f"\n[可在 UI 查看] 工作流 {wf_id} / 运行 {run_id}")


asyncio.run(main())
