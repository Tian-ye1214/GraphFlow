import asyncio
import json

from sqlalchemy import select

from app import crypto
from app.engine import runner
from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)
from app.services import llm

GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}


async def make_run(session_factory, graph=None, rows=3) -> int:
    async with session_factory() as s:
        u = User(username=f"runner{id(graph)}")
        s.add(u)
        await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="qwen", base_url="http://x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc)
        await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=rows)
        s.add(ds)
        await s.flush()
        for i in range(rows):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": f"问{i}"}, ensure_ascii=False)))
        g = json.loads(json.dumps(graph or GRAPH))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] in ("llm_synth", "qc"):
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=json.dumps(g))
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(g))
        s.add(ver)
        await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run)
        await s.commit()
        return run.id


def patch_chat(monkeypatch, fn=None):
    calls: list[str] = []

    async def fake(mc, system, user, params=None, retries=3):
        calls.append(user)
        if fn:
            return fn(user)
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake)
    return calls


async def run_it(session_factory, run_id, cancel=None):
    await runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel or asyncio.Event())


async def get_run(session_factory, run_id) -> Run:
    async with session_factory() as s:
        return await s.get(Run, run_id)


RESCAN_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "qc", "type": "qc",
         "config": {"model_config_id": 0, "user_prompt": "判定:{{a}}", "max_rounds": 2}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "qc", "kind": "normal"},
              {"source": "qc", "target": "out", "kind": "normal"},
              {"source": "qc", "target": "gen", "kind": "rescan"}],
}


DROP_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "drop_columns": ["q"], "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}


async def test_drop_columns_excluded_from_node_output(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory, DROP_GRAPH, rows=2)
    await run_it(session_factory, run_id)
    rows = await runner._node_outputs(session_factory, run_id, "gen")
    assert len(rows) == 2
    for r in rows:
        assert "q" not in r        # 输入列 q 被本节点删除，不落库
        assert r["a"]              # 产出列 a 保留


def _rescan_fn(persistent):
    def fn(user):
        if user.startswith("判定:"):  # 质检判定调用：含 bad 即不通过
            bad = "bad" in user
            return json.dumps({"status": "failed" if bad else "pass", "reason": "含bad" if bad else ""}), \
                {"prompt_tokens": 1, "completion_tokens": 1}
        # 生成调用：问1 首次（或 persistent 时永远）产坏值
        first = "质检未通过" not in user
        if "问1" in user and (persistent or first):
            return "bad答", {"prompt_tokens": 1, "completion_tokens": 1}
        return "good答", {"prompt_tokens": 1, "completion_tokens": 1}
    return fn


async def test_rescan_regenerates_failed_rows(session_factory, monkeypatch):
    patch_chat(monkeypatch, _rescan_fn(persistent=False))
    run_id = await make_run(session_factory, graph=RESCAN_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    out_rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert len(out_rows) == 3 and all("bad" not in r["a"] for r in out_rows)
    # gen 首轮 3 行(3) + qc 折叠：判定 3 首轮+1 复判=4，回扫重生成 1 → qc usage 5；合计 8
    assert json.loads(run.stats_json) == {"prompt_tokens": 8, "completion_tokens": 8}
    async with session_factory() as s:
        qc_rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "qc"))).scalar_one()
    assert qc_rec.qc_round == 1


async def test_rescan_drops_persistent_failures(session_factory, monkeypatch):
    patch_chat(monkeypatch, _rescan_fn(persistent=True))
    run_id = await make_run(session_factory, graph=RESCAN_GRAPH)
    await run_it(session_factory, run_id)
    out_rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert {r["q"] for r in out_rows} == {"问0", "问2"}  # 问1 始终不过被丢弃
    async with session_factory() as s:
        qc_rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "qc"))).scalar_one()
    assert qc_rec.qc_round == 2  # 用满 max_rounds


async def test_happy_path(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    assert json.loads(run.stats_json) == {"prompt_tokens": 3, "completion_tokens": 6}
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 3
    assert out_rows[0]["a"] == "答[Q:问0]"
    async with session_factory() as s:
        states = {ns.node_id: ns for ns in (await s.execute(
            select(RunNodeState).where(RunNodeState.run_id == run_id))).scalars()}
    assert states["gen"].total == 3 and states["gen"].done == 3 and states["gen"].failed == 0
    assert states["out"].status == "done"


async def test_row_failure_continues(session_factory, monkeypatch):
    def fn(user):
        if "问1" in user:
            raise RuntimeError("坏行")
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"  # 单行失败不挂任务
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 2
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen", RunRow.status == "failed"))).scalar_one()
    assert "坏行" in rec.error and rec.row_idx == 1


async def test_cancel_before_llm(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    ev = asyncio.Event()
    ev.set()
    await run_it(session_factory, run_id, cancel=ev)
    assert (await get_run(session_factory, run_id)).status == "cancelled"
    assert calls == []


async def test_resume_skips_done_rows(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    async with session_factory() as s:  # 预置 idx0 已完成（模拟上次中断）
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "问0", "a": "旧结果"}], ensure_ascii=False)))
        await s.commit()
    await run_it(session_factory, run_id)
    assert sorted(calls) == ["Q:问1", "Q:问2"]  # 只跑了未完成的两行
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["a"] for r in out_rows} == {"旧结果", "答[Q:问1]", "答[Q:问2]"}


async def test_barrier_failure_fails_run(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"].insert(1, {"id": "proc", "type": "auto_process",
                              "config": {"operations": [{"op": "cast", "column": "q", "to": "int"}]}})
    graph["edges"] = [{"source": "in", "target": "proc", "kind": "normal"},
                      {"source": "proc", "target": "gen", "kind": "normal"},
                      {"source": "gen", "target": "out", "kind": "normal"}]
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed"
    assert "invalid literal" in run.error


async def test_fanout_multiplies_rows(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][1]["config"]["fanout_n"] = 2
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "out")) == 6


async def test_output_save_as_dataset(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["config"] = {"save_as_dataset": True, "dataset_name": "结果集"}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalar_one()
    assert ds.source == "run" and ds.row_count == 3


async def test_barrier_crash_window_repairs_state(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    async with session_factory() as s:  # 模拟崩溃窗口：单元已 done，节点状态卡在 running
        s.add(RunRow(run_id=run_id, node_id="in", row_idx=0, status="done",
                     data_json=json.dumps([{"q": f"问{i}"} for i in range(3)], ensure_ascii=False)))
        s.add(RunNodeState(run_id=run_id, node_id="in", status="running", total=1, done=0))
        await s.commit()
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == "in"))).scalar_one()
    assert ns.status == "done" and ns.done == 1


DIAMOND_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "A", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "A:{{q}}", "output_column": "a"}},
        {"id": "B", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "B:{{q}}", "output_column": "b"}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "A", "kind": "normal"},
              {"source": "in", "target": "B", "kind": "normal"},
              {"source": "A", "target": "out", "kind": "normal"},
              {"source": "B", "target": "out", "kind": "normal"}],
}


async def test_merge_branches_combines_columns(session_factory, monkeypatch):
    """并行两分支汇入一个节点：按行合并成每行都含 a&b（不是堆叠成 6 行）。"""
    patch_chat(monkeypatch, lambda u: (("a值" if u.startswith("A:") else "b值"),
                                       {"prompt_tokens": 1, "completion_tokens": 1}))
    run_id = await make_run(session_factory, graph=DIAMOND_GRAPH, rows=3)
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 3                                    # 合并而非堆叠
    assert all({"q", "a", "b"} <= set(r) for r in out_rows)     # 每行都同时拥有 a 和 b
    assert out_rows[0] == {"q": "问0", "a": "a值", "b": "b值"}


async def test_merge_row_count_mismatch_fails(session_factory, monkeypatch):
    """某分支 fanout 改了行数 → 两支对不齐 → 结构性报错，整 run failed。"""
    patch_chat(monkeypatch, lambda u: ("v", {"prompt_tokens": 1, "completion_tokens": 1}))
    graph = json.loads(json.dumps(DIAMOND_GRAPH))
    next(n for n in graph["nodes"] if n["id"] == "A")["config"]["fanout_n"] = 2
    run_id = await make_run(session_factory, graph=graph, rows=3)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed" and "行数不一致" in run.error


async def test_merge_column_conflict_fails(session_factory, monkeypatch):
    """两分支产出同名列且取值不同 → 合并冲突 → 整 run failed。"""
    patch_chat(monkeypatch, lambda u: (("甲" if u.startswith("A:") else "乙"),
                                       {"prompt_tokens": 1, "completion_tokens": 1}))
    graph = json.loads(json.dumps(DIAMOND_GRAPH))
    for n in graph["nodes"]:
        if n["id"] in ("A", "B"):
            n["config"]["output_column"] = "ans"               # 同名产出列
    run_id = await make_run(session_factory, graph=graph, rows=2)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed" and "取值不同" in run.error


def test_merge_conflict_distinguishes_int_bool_float():
    """合并冲突检测须比类型：0/False、1/True、0/0.0 数值相等但语义不同，应报冲突而非静默用一支覆盖。"""
    import pytest
    for a, b in ((0, False), (1, True), (0, 0.0)):
        with pytest.raises(ValueError, match="取值不同"):
            runner._merge_branches("m", [[{"k": a}], [{"k": b}]])
    assert runner._merge_branches("m", [[{"k": 5}], [{"k": 5}]]) == [{"k": 5}]  # 同类型相等不报(回归)


async def test_rerun_output_upserts_dataset_no_duplicate(session_factory, monkeypatch):
    """重算 output 节点(save_as_dataset)按 run_id+name upsert，不产生重复同名数据集。"""
    from sqlalchemy import delete as sa_delete
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["config"] = {"save_as_dataset": True, "dataset_name": "结果集"}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    async with session_factory() as s:   # 模拟 rerun-failed：重置并重算 output 节点
        await s.execute(sa_delete(RunRow).where(RunRow.run_id == run_id, RunRow.node_id == "out"))
        await s.execute(sa_delete(RunNodeState).where(RunNodeState.run_id == run_id, RunNodeState.node_id == "out"))
        run = await s.get(Run, run_id); run.status = "queued"; await s.commit()
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        dss = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalars().all()
    assert len(dss) == 1 and dss[0].row_count == 3   # 仅一份，且为完整 3 行


async def test_output_drop_columns_applied_to_saved_dataset(session_factory, monkeypatch):
    """output 节点 drop_columns 须同样作用于 save_as_dataset 落库数据集（被删列不泄漏进训练集）。"""
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["config"] = {"save_as_dataset": True, "dataset_name": "去列集", "drop_columns": ["q"]}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.name == "去列集"))).scalar_one()
        rows = (await s.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds.id))).scalars().all()
    saved = [json.loads(r.data_json) for r in rows]
    assert saved and all("q" not in r and "a" in r for r in saved)   # 被删列 q 不进落库集，产出列 a 保留
    assert json.loads(ds.columns_json) == ["a"]


async def test_rescan_fanout_no_inflation(session_factory, monkeypatch):
    """回扫目标带 fanout_n>1：回扫应一行换一行，不得让通过数超过输入、failed 计负、产物翻倍。"""
    def fn(user):
        if user.startswith("判定:"):
            bad = "bad" in user
            return json.dumps({"status": "failed" if bad else "pass", "reason": "bad" if bad else ""}), \
                {"prompt_tokens": 1, "completion_tokens": 1}
        if "质检未通过" in user:           # 回扫重生成
            return "good", {"prompt_tokens": 1, "completion_tokens": 1}
        return "bad", {"prompt_tokens": 1, "completion_tokens": 1}  # 首轮生成

    patch_chat(monkeypatch, fn)
    graph = json.loads(json.dumps(RESCAN_GRAPH))
    graph["nodes"][1]["config"]["fanout_n"] = 2
    run_id = await make_run(session_factory, graph=graph, rows=1)
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == "qc"))).scalar_one()
    out_rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert ns.failed >= 0                            # 不得为负
    assert ns.total == 2 and ns.done == 2            # 1 输入 × fanout2 = 2
    assert len(out_rows) == 2                        # 回扫不再翻倍


async def test_qc_resume_preserves_pass_fail_counts(session_factory, monkeypatch):
    """已完成 QC 节点再执行(续跑)，应保留真实通过/拒绝计数，不被清成全通过。"""
    def fn(user):
        if user.startswith("判定:"):
            ok = "问1" not in user
            return json.dumps({"status": "pass" if ok else "failed", "reason": "" if ok else "坏"}), \
                {"prompt_tokens": 1, "completion_tokens": 1}
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    graph = json.loads(json.dumps(RESCAN_GRAPH))
    graph["edges"] = [e for e in graph["edges"] if e["kind"] != "rescan"]  # 去回扫，问1 直接被拒丢弃
    run_id = await make_run(session_factory, graph=graph, rows=3)
    await run_it(session_factory, run_id)

    async def qc_counts():
        async with session_factory() as s:
            ns = (await s.execute(select(RunNodeState).where(
                RunNodeState.run_id == run_id, RunNodeState.node_id == "qc"))).scalar_one()
            return ns.total, ns.done, ns.failed

    assert await qc_counts() == (3, 2, 1)
    await run_it(session_factory, run_id)            # 再执行 = 续跑
    assert await qc_counts() == (3, 2, 1)            # 仍保留，不被清成 (3,3,0)


async def test_hard_interrupt_aborts_inflight(session_factory, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()  # release 永不置位：只有硬中断能解开

    async def fake(mc, system, user, params=None, retries=3):
        started.set()
        await release.wait()  # 阻塞在途；非取消则永远不返回
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][1]["config"]["concurrency"] = 1
    run_id = await make_run(session_factory, graph=graph)
    cancel = asyncio.Event()
    task = asyncio.create_task(
        runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel))
    await asyncio.wait_for(started.wait(), timeout=2)  # 行 0 已在途
    cancel.set()
    await asyncio.wait_for(task, timeout=2)  # 必须迅速结束（被中止），而非卡在 release 上
    run = await get_run(session_factory, run_id)
    assert run.status == "cancelled"
    async with session_factory() as s:
        recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen"))).scalars().all()
    assert all(r.status != "done" for r in recs)  # 在途行被中止，不落库


async def test_llm_passes_through_all_columns(session_factory, monkeypatch):
    """10 列输入，LLM 只引用其中 1 列产出 ans —— 输出节点每行应含全部 10 列 + ans（保存全面）。"""
    patch_chat(monkeypatch)
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": []}},
            {"id": "gen", "type": "llm_synth",
             "config": {"model_config_id": 0, "user_prompt": "Q:{{c0}}", "output_column": "ans"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "out", "kind": "normal"}],
    }
    async with session_factory() as s:
        u = User(username="passthru")
        s.add(u)
        await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="q", base_url="http://x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc)
        await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=2)
        s.add(ds)
        await s.flush()
        for i in range(2):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps(
                {f"c{j}": f"v{i}_{j}" for j in range(10)}, ensure_ascii=False)))
        g = json.loads(json.dumps(graph))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] == "llm_synth":
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=json.dumps(g))
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(g))
        s.add(ver)
        await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run)
        await s.commit()
        run_id = run.id
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 2
    for r in out_rows:
        assert all(f"c{j}" in r for j in range(10))   # 全部 10 列透传到最终保存
        assert "ans" in r                              # 产出列也在
