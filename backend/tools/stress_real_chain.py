"""真实链路压测：必须真实调用库里配置的模型（DeepSeek）跑整条 run 管线，用以暴露 mock 测不到的 bug。

每个场景建数据集+工作流并经 manager 真跑（真实 LLM 调用），再统计每节点 done/failed 与样本错误。
绝不输出任何密钥材料。

用法（backend 目录）：
    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/stress_real_chain.py <scenario> [model_id]
    scenario ∈ json | volume | rescan | cancel | all
"""
import asyncio
import json
import sys
import time

from sqlalchemy import select

from app.agent import goal_loop
from app.db import get_session_factory, init_db
from app.engine import runner
from app.engine.manager import manager
from app.models import ModelConfig, Run, RunRow, User, Workflow
from app.routers.datasets import create_dataset
from app.services import run_service


async def _pick_model(s, want):
    mcs = (await s.execute(select(ModelConfig).order_by(ModelConfig.id))).scalars().all()
    return (next((m for m in mcs if m.id == want), None) if want
            else next((m for m in mcs if "deepseek" in m.base_url.lower()), None))


async def _mk_wf(s, user_id, name, graph):
    wf = Workflow(user_id=user_id, name=name, graph_json=json.dumps(graph))
    s.add(wf)
    await s.commit()
    return wf.id


async def _run(sf, user_id, wf_id, timeout=300):
    run_id = await run_service.enqueue_run(sf, user_id, wf_id)
    try:
        await asyncio.wait_for(manager.wait(run_id), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  [超时 {timeout}s] 取消 run {run_id}")
        manager.cancel(run_id)
        await manager.wait(run_id)
    async with sf() as s:
        run = await s.get(Run, run_id)
    return run_id, run


async def _node_rows(sf, run_id, node_id):
    async with sf() as s:
        rows = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node_id))).scalars().all()
    return [r for r in rows if r.status == "done"], [r for r in rows if r.status == "failed"]


def _linear_json_graph(ds_id, mc_id, with_json_mode):
    cfg = {"model_config_id": mc_id, "output_mode": "json", "output_columns": ["sentiment", "reason"],
           "user_prompt": "对下面文本做情感分析，只输出一个 JSON 对象，含字段 sentiment(positive/negative/neutral) 与 reason：\n{{text}}",
           "concurrency": 4, "retries": 2}
    if with_json_mode:
        cfg["params"] = {"json_mode": True}
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": cfg},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "out", "kind": "normal"}],
    }


async def scenario_json(sf, user_id, mc_id):
    """synth output_mode=json：对比「不设 json_mode」与「设 json_mode」两路真实跑，看解析失败率。"""
    print("\n=== 场景 json：synth JSON 输出解析健壮性 ===")
    texts = ["今天阳光明媚但我心情低落。", "这家餐厅太棒了，强烈推荐！", "服务一般，价格偏贵。",
             "我对结果不予置评。", "🎉🎉🎉"]
    rows = [{"text": t} for t in texts]
    async with sf() as s:
        ds = await create_dataset(s, user_id, "压测-json", rows, source="upload")
        ds_id = ds.id
    for with_jm in (False, True):
        async with sf() as s:
            wf_id = await _mk_wf(s, user_id, f"压测-json-{'有' if with_jm else '无'}jsonmode",
                                 _linear_json_graph(ds_id, mc_id, with_jm))
        run_id, run = await _run(sf, user_id, wf_id, timeout=180)
        done, failed = await _node_rows(sf, run_id, "gen")
        tag = "json_mode=True " if with_jm else "json_mode=未设"
        print(f"  [{tag}] run={run_id} status={run.status} done={len(done)} failed={len(failed)}")
        if failed:
            print(f"    失败样本错误：{failed[0].error[:200]}")
        if done:
            print(f"    成功样本输出键：{sorted(json.loads(done[0].data_json)[0].keys())}")


async def scenario_volume(sf, user_id, mc_id):
    """高并发体量：30 行 concurrency=16 真实跑，看并发/限流/重试下是否优雅完成。"""
    print("\n=== 场景 volume：高并发体量压测 ===")
    async with sf() as s:
        user = await s.get(User, user_id)
        cap = user.max_llm_concurrency
        rows = [{"q": f"用一句话回答第{i}个问题：{i} 的平方是多少？"} for i in range(30)]
        ds = await create_dataset(s, user_id, "压测-volume", rows, source="upload")
        ds_id = ds.id
    print(f"  用户并发上限 max_llm_concurrency={cap}（实际并发=min(节点16, {cap})）")
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "a", "concurrency": 16, "retries": 3,
                "user_prompt": "{{q}}"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "out", "kind": "normal"}],
    }
    async with sf() as s:
        wf_id = await _mk_wf(s, user_id, "压测-volume", graph)
    t0 = time.monotonic()
    run_id, run = await _run(sf, user_id, wf_id, timeout=300)
    dt = time.monotonic() - t0
    done, failed = await _node_rows(sf, run_id, "gen")
    print(f"  run={run_id} status={run.status} done={len(done)} failed={len(failed)} 用时={dt:.1f}s")
    if failed:
        print(f"    失败样本错误：{failed[0].error[:200]}")


async def scenario_rescan(sf, user_id, mc_id):
    """真实多轮回扫：严格质检逼出真实失败 → rescan 重生最多 2 轮，活体走目标循环。"""
    print("\n=== 场景 rescan：真实质检失败 → 多轮回扫 ===")
    rows = [{"q": q} for q in [
        "中国的首都是哪里？", "水的化学式是什么？", "1+1 等于几？", "地球有几个卫星？", "光速大约是多少？"]]
    async with sf() as s:
        ds = await create_dataset(s, user_id, "压测-rescan", rows, source="upload")
        ds_id = ds.id
    judge_sys = "你是格式质检员。只有当答案是【恰好一个 emoji、不含任何文字/数字/标点】时才算通过。"
    judge_user = '问题：{{q}}\n答案：{{answer}}\n返回 JSON：{"pass": true/false, "reason": "理由"}'
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "answer", "concurrency": 4, "retries": 2,
                "user_prompt": "回答问题：{{q}}"}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 2, "concurrency": 4,
                "system_prompt": judge_sys, "user_prompt": judge_user, "params": {"temperature": 0}}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "qc", "kind": "normal"},
                  {"source": "qc", "target": "out", "kind": "normal"},
                  {"source": "qc", "target": "gen", "kind": "rescan"}],
    }
    async with sf() as s:
        wf_id = await _mk_wf(s, user_id, "压测-rescan", graph)
    run_id, run = await _run(sf, user_id, wf_id, timeout=300)
    rate = await run_service.first_round_rate(sf, run_id)
    fails = await run_service.sample_failures(sf, run_id, n=20)
    done, failed = await _node_rows(sf, run_id, "qc")
    qc_round = done[0].qc_round if done else None
    print(f"  run={run_id} status={run.status} 首轮通过率={rate} 回扫轮数={qc_round} "
          f"最终通过={len(done) and len(json.loads(done[0].data_json))} 最终失败={len(fails)}")
    for f in fails[:3]:
        rs = [pm.get("reason") for pm in f["reasons"]] if f["reasons"] else f["reasons"]
        print(f"    ✗ {f['sample'].get('q')!r} ans={f['sample'].get('answer')!r} → {rs}")


async def scenario_cancel(sf, user_id, mc_id):
    """取消竞态：体量 run 起跑后 ~1.5s 取消，验证 status=cancelled 且未全部完成（在途请求被中断）。"""
    print("\n=== 场景 cancel：运行中取消 ===")
    rows = [{"q": f"详细解释第{i}个概念（200字以上）：概念{i}"} for i in range(20)]
    async with sf() as s:
        ds = await create_dataset(s, user_id, "压测-cancel", rows, source="upload")
        ds_id = ds.id
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "a", "concurrency": 4, "retries": 1,
                "user_prompt": "{{q}}"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "out", "kind": "normal"}],
    }
    async with sf() as s:
        wf_id = await _mk_wf(s, user_id, "压测-cancel", graph)
    run_id = await run_service.enqueue_run(sf, user_id, wf_id)
    await asyncio.sleep(1.5)
    manager.cancel(run_id)
    await manager.wait(run_id)
    async with sf() as s:
        run = await s.get(Run, run_id)
    done, failed = await _node_rows(sf, run_id, "gen")
    print(f"  run={run_id} status={run.status} done={len(done)}/20（取消后应 <20，且 status=cancelled）")


async def scenario_fanout(sf, user_id, mc_id):
    """fanout_n=3：每个输入行扇出 3 次生成，验证输出行数为输入的 3 倍、下游展平正确。"""
    print("\n=== 场景 fanout：扇出生成行倍增 ===")
    rows = [{"q": "给我一个中文成语，只回成语本身"}, {"q": "给我一个英文单词，只回单词本身"}]
    async with sf() as s:
        ds = await create_dataset(s, user_id, "压测-fanout", rows, source="upload")
        ds_id = ds.id
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "a", "concurrency": 4, "retries": 2,
                "fanout_n": 3, "params": {"temperature": 1.3}, "user_prompt": "{{q}}"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "out", "kind": "normal"}],
    }
    async with sf() as s:
        wf_id = await _mk_wf(s, user_id, "压测-fanout", graph)
    run_id, run = await _run(sf, user_id, wf_id, timeout=180)
    out_rows = await runner._node_outputs(sf, run_id, "out")
    print(f"  run={run_id} status={run.status} 输入行=2 fanout=3 → 输出行={len(out_rows)}（应=6）")
    for r in out_rows:
        print(f"    {r.get('q')!r} → {r.get('a')!r}")


SCENARIOS = {"json": scenario_json, "volume": scenario_volume, "rescan": scenario_rescan,
             "cancel": scenario_cancel, "fanout": scenario_fanout}


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    want = int(sys.argv[2]) if len(sys.argv) > 2 else None
    await init_db()
    sf = get_session_factory()
    async with sf() as s:
        mc = await _pick_model(s, want)
        if mc is None:
            print("找不到模型配置"); return
        user_id, mc_id = mc.user_id, mc.id
    print(f"[模型] id={mc_id} model_name={mc.model_name!r} base_url={mc.base_url!r} user_id={user_id}")
    todo = list(SCENARIOS) if which == "all" else [which]
    for name in todo:
        await SCENARIOS[name](sf, user_id, mc_id)
    print("\n[完成] 压测产物在库里，可在 UI 查看/删除。")


asyncio.run(main())
