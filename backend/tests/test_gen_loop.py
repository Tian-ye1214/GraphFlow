import asyncio
import json as _json
import math

import pytest
from sqlalchemy import select

from app import crypto
from app.engine import runner
from app.engine.graph import parse_graph
from app.engine.runner import _gen_batch_size, _generation_chain
from app.models import ModelConfig, Run, RunRow, User, Workflow, WorkflowVersion
from app.services import llm

_SYN = {"id": "g", "type": "llm_synth", "config": {}}
_E = lambda a, b: {"source": a, "target": b, "kind": "normal"}  # noqa: E731


def _chain(nodes, edges):
    return _generation_chain(parse_graph({"nodes": nodes, "edges": edges}))


_UCNT = [0]   # 模块级计数器：保证单个测试内多次建用户的 username 唯一（每测试 DB 全新，跨测试复用无碍）


async def _mk_user_model(session_factory):
    _UCNT[0] += 1
    async with session_factory() as s:
        u = User(username=f"gen{_UCNT[0]}")
        s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="q", base_url="http://x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc); await s.commit()
        return u.id, mc.id


async def make_gen_run(session_factory, graph) -> tuple[int, int]:
    """建一个「无 input 节点」的工作流+run；给 llm/http/qc 节点填 model_config_id。返回 (run_id, user_id)。"""
    uid, mc_id = await _mk_user_model(session_factory)
    g = _json.loads(_json.dumps(graph))
    for n in g["nodes"]:
        if n["type"] in ("llm_synth", "qc", "http_fetch"):
            n["config"].setdefault("model_config_id", mc_id)
    async with session_factory() as s:
        wf = Workflow(user_id=uid, name="wf", graph_json=_json.dumps(g))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=_json.dumps(g))
        s.add(ver); await s.flush()
        run = Run(user_id=uid, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run); await s.commit()
        return run.id, uid


def patch_chat(monkeypatch, fn=None):
    calls = []

    async def fake(mc, system, user, params=None, retries=3):
        calls.append(user)
        return fn(user) if fn else (f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2})

    monkeypatch.setattr(llm, "chat", fake)
    return calls


async def run_it(session_factory, run_id, cancel=None):
    await runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel or asyncio.Event())


async def get_run(session_factory, run_id) -> Run:
    async with session_factory() as s:
        return await s.get(Run, run_id)


def test_gen_batch_size_first_batch_assumes_full_yield():
    # 首批无产率数据(generated=0)→按 yield=1.0：ceil(gap/fanout)
    assert _gen_batch_size(gap=10, fanout=1, accepted=0, generated=0) == 10
    assert _gen_batch_size(gap=10, fanout=2, accepted=0, generated=0) == 5
    assert _gen_batch_size(gap=3, fanout=2, accepted=0, generated=0) == 2   # ceil(3/2)


def test_gen_batch_size_scales_by_observed_yield():
    # 已生成 10 候选、接收 5 → yield=0.5 → 缺口按 1/0.5=2 倍放大
    assert _gen_batch_size(gap=4, fanout=1, accepted=5, generated=10) == 8


def test_gen_batch_size_clamps_low_yield_to_floor():
    # 极低产率(0.05)被钳到 0.2 下限：最多 5×缺口，不无界暴量
    assert _gen_batch_size(gap=4, fanout=1, accepted=1, generated=20) == 20   # ceil(4/0.2)


def test_gen_batch_size_never_zero():
    # 缺口 1、扇出 10 → ceil(1/10)=1，至少 1 防死循环
    assert _gen_batch_size(gap=1, fanout=10, accepted=0, generated=0) == 1


def test_generation_chain_none_for_normal_graph():
    # 有 input 节点喂生成节点 → 生成节点有父 → 非无输入链 → None（走普通路径）
    nodes = [{"id": "in", "type": "input", "config": {}}, _SYN,
             {"id": "o", "type": "output", "config": {"count": 5}}]
    assert _chain(nodes, [_E("in", "g"), _E("g", "o")]) is None


def test_generation_chain_linear_synth_output():
    nodes = [_SYN, {"id": "o", "type": "output", "config": {"count": 5}}]
    chain = _chain(nodes, [_E("g", "o")])
    assert [n.id for n in chain] == ["g", "o"]


def test_generation_chain_with_qc():
    nodes = [_SYN, {"id": "q", "type": "qc", "config": {}},
             {"id": "o", "type": "output", "config": {"count": 5}}]
    chain = _chain(nodes, [_E("g", "q"), _E("q", "o")])
    assert [n.id for n in chain] == ["g", "q", "o"]


def test_generation_chain_requires_output_count():
    nodes = [_SYN, {"id": "o", "type": "output", "config": {}}]
    with pytest.raises(ValueError, match="接收数量"):
        _chain(nodes, [_E("g", "o")])


def test_generation_chain_rejects_count_non_positive():
    nodes = [_SYN, {"id": "o", "type": "output", "config": {"count": 0}}]
    with pytest.raises(ValueError, match="接收数量"):
        _chain(nodes, [_E("g", "o")])


def test_generation_chain_rejects_fork():
    nodes = [_SYN, {"id": "o1", "type": "output", "config": {"count": 5}},
             {"id": "o2", "type": "output", "config": {"count": 5}}]
    with pytest.raises(ValueError, match="单链"):
        _chain(nodes, [_E("g", "o1"), _E("g", "o2")])


def test_generation_chain_input_present_returns_none():
    # 图含 input 节点 = 普通工作流 → 即便有游离生成节点也返回 None(走单遍 topo)，不误判/不报错(防回归)
    nodes = [{"id": "in", "type": "input", "config": {}}, _SYN,
             {"id": "o", "type": "output", "config": {"count": 5}},
             {"id": "stray", "type": "llm_synth", "config": {}}]   # 无任何边的游离生成节点
    assert _chain(nodes, [_E("in", "g"), _E("g", "o")]) is None


def test_generation_chain_rejects_stray_node():
    nodes = [_SYN, {"id": "o", "type": "output", "config": {"count": 5}},
             {"id": "stray", "type": "llm_synth", "config": {}}]
    # stray 也无父 → 两个起始节点 → 报错
    with pytest.raises(ValueError, match="一个起始"):
        _chain(nodes, [_E("g", "o")])


async def test_per_row_node_row_base_writes_offset(session_factory, monkeypatch):
    """_run_per_row_node row_base=5：3 行写到 row_idx 5/6/7；_batch_outputs 按区间读回。"""
    from app.engine.graph import Node
    patch_chat(monkeypatch)
    uid, mc_id = await _mk_user_model(session_factory)
    async with session_factory() as s:
        run = Run(user_id=uid, workflow_id=0, workflow_version_id=0)
        s.add(run); await s.commit(); run_id = run.id
    async with session_factory() as s:
        mc = await s.get(ModelConfig, mc_id)
    node = Node(id="g", type="llm_synth", config={"user_prompt": "U:{{x}}", "output_column": "a"})
    inputs = [{"x": f"v{i}"} for i in range(3)]
    await runner._run_per_row_node(
        session_factory, run_id, uid, node, inputs, asyncio.Event(),
        row_coro=lambda i: runner.nodes.run_llm_synth_row(node.config, inputs[i], mc, asyncio.Semaphore(8)),
        log_source="synth", row_base=5)
    async with session_factory() as s:
        idxs = sorted((await s.execute(select(RunRow.row_idx).where(
            RunRow.run_id == run_id, RunRow.node_id == "g"))).scalars().all())
    assert idxs == [5, 6, 7]
    out = await runner._batch_outputs(session_factory, run_id, "g", 5, 8)
    assert len(out) == 3 and all(r["a"].startswith("答[U:v") for r in out)


SYNTH_OUT = {
    "nodes": [
        {"id": "g", "type": "llm_synth",
         "config": {"user_prompt": "造一条", "output_column": "a", "concurrency": 4, "retries": 1}},
        {"id": "o", "type": "output", "config": {"count": 4}},
    ],
    "edges": [{"source": "g", "target": "o", "kind": "normal"}],
}


async def test_inputless_synth_generates_exact_count(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id, _ = await make_gen_run(session_factory, SYNTH_OUT)
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 4                                  # 恰好 count 条
    assert len(calls) == 4                                # 无质检：4 次生成即够
    async with session_factory() as s:
        from app.models import RunNodeState
        o = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == "o"))).scalar_one()
    assert o.status == "done" and o.total == 4 and o.done == 4


async def test_inputless_synth_fanout(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    graph = _json.loads(_json.dumps(SYNTH_OUT))
    graph["nodes"][0]["config"]["fanout_n"] = 2
    graph["nodes"][1]["config"]["count"] = 4
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 4 and len(calls) == 4             # ceil(4/2)=2 种子 ×2 扇出，每种子 2 次调用 → 4 calls


async def test_inputless_http_runs_once_as_topo_source(session_factory, monkeypatch):
    """无 input 节点的 http_fetch 改走 topo 数据源（非生成循环）：触发一次取数→产 1 行。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, _json.dumps({"v": "ok"})
    from app.services import http
    monkeypatch.setattr(http, "fetch", fake_fetch)
    graph = {
        "nodes": [
            {"id": "h", "type": "http_fetch",
             "config": {"url": "http://x/api", "extract": {"v": "v"}}},
            {"id": "o", "type": "output", "config": {}},
        ],
        "edges": [{"source": "h", "target": "o", "kind": "normal"}],
    }
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 1 and out[0]["v"] == "ok"


GEN_QC = {
    "nodes": [
        {"id": "g", "type": "llm_synth",
         "config": {"user_prompt": "造", "output_column": "a", "concurrency": 4, "retries": 1}},
        {"id": "q", "type": "qc",
         "config": {"user_prompt": "判:{{a}}", "max_rounds": 1, "concurrency": 4}},
        {"id": "o", "type": "output", "config": {"count": 4}},
    ],
    "edges": [{"source": "g", "target": "q", "kind": "normal"},
              {"source": "q", "target": "o", "kind": "normal"}],
}


async def test_inputless_gen_with_qc_reaches_count(session_factory, monkeypatch):
    """质检按生成序拒一半：要凑够 count=4 接收，必须生成 > 4。"""
    seq = {"n": 0}

    def fn(user):
        if user.startswith("判:"):
            seq["n"] += 1
            ok = seq["n"] % 2 == 1                          # 奇数过、偶数拒 → 约一半
            return (_json.dumps({"status": "pass" if ok else "failed", "reason": "x"}),
                    {"prompt_tokens": 1, "completion_tokens": 1})
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    run_id, _ = await make_gen_run(session_factory, GEN_QC)
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 4                                    # 接收恰好 count
    assert all(r.get("qc_status") == "pass" for r in out)
    gen = await runner._node_outputs(session_factory, run_id, "g")
    assert len(gen) > 4                                     # 生成 > count（被淘汰的占了额外生成）
    async with session_factory() as s:
        from app.models import QcMetric
        metrics = (await s.execute(select(QcMetric).where(
            QcMetric.run_id == run_id))).scalars().all()
    assert sum(m.total for m in metrics) == len(gen)        # QcMetric 跨批聚合=总判定数


async def test_inputless_low_yield_converges(session_factory, monkeypatch):
    """产率仅 1/3 也能凑够 count（批量自适应放大缺口）：接收=count，生成≥count。"""
    seq = {"n": 0}

    def fn(user):
        if user.startswith("判:"):
            seq["n"] += 1
            ok = seq["n"] % 3 == 0
            return (_json.dumps({"status": "pass" if ok else "failed", "reason": "x"}),
                    {"prompt_tokens": 1, "completion_tokens": 1})
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    graph = _json.loads(_json.dumps(GEN_QC))
    graph["nodes"][2]["config"]["count"] = 6
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 6
    assert len(await runner._node_outputs(session_factory, run_id, "g")) >= 6


async def test_inputless_cancel_mid_loop_stops(session_factory, monkeypatch):
    """循环中途取消：run=cancelled，已接收行保留，输出未写满。"""
    cancel = asyncio.Event()
    n = {"c": 0}

    async def fake(mc, system, user, params=None, retries=3):
        n["c"] += 1
        if n["c"] >= 3:
            cancel.set()                                    # 生成几次后请求取消
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    graph = _json.loads(_json.dumps(SYNTH_OUT))
    graph["nodes"][0]["config"]["concurrency"] = 1
    graph["nodes"][1]["config"]["count"] = 100              # 故意很大，必被取消打断
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id, cancel=cancel)
    assert (await get_run(session_factory, run_id)).status == "cancelled"
    assert len(await runner._node_outputs(session_factory, run_id, "o")) < 100


async def test_inputless_auto_process_in_chain(session_factory, monkeypatch):
    """链中含 auto_process（每批转换）：start→proc→output(count) 跑通、达 count。
    用确有的 concat 算子把列 a 复制到 A（_OPS = dedup/filter/rename/drop/concat/cast/sample/shuffle）。"""
    patch_chat(monkeypatch, lambda u: ("xy", {"prompt_tokens": 1, "completion_tokens": 1}))
    graph = {
        "nodes": [
            {"id": "g", "type": "llm_synth",
             "config": {"user_prompt": "造", "output_column": "a", "concurrency": 4, "retries": 1}},
            {"id": "p", "type": "auto_process",
             "config": {"operations": [{"op": "concat", "columns": ["a"], "target": "A", "sep": ""}]}},
            {"id": "o", "type": "output", "config": {"count": 3}},
        ],
        "edges": [{"source": "g", "target": "p", "kind": "normal"},
                  {"source": "p", "target": "o", "kind": "normal"}],
    }
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 3 and all(r.get("A") == "xy" for r in out)


async def test_inputless_missing_count_fails_run(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = _json.loads(_json.dumps(SYNTH_OUT))
    graph["nodes"][1]["config"] = {}                        # output 不设 count
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed" and "接收数量" in (run.error or "")


async def test_inputless_fork_fails_run(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = {
        "nodes": [
            {"id": "g", "type": "llm_synth", "config": {"user_prompt": "造", "output_column": "a"}},
            {"id": "o1", "type": "output", "config": {"count": 2}},
            {"id": "o2", "type": "output", "config": {"count": 2}},
        ],
        "edges": [{"source": "g", "target": "o1", "kind": "normal"},
                  {"source": "g", "target": "o2", "kind": "normal"}],
    }
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed" and "单链" in (run.error or "")


def test_attach_root_trace_start_offset():
    """attach_root_trace start 偏移：行号从 start 起，使生成循环跨批 root trace 不撞。"""
    from app.services.trace import TRACE_ID_KEY, attach_root_trace
    a = attach_root_trace([{}, {}], run_id=7, node_id="g", start=0)
    b = attach_root_trace([{}, {}], run_id=7, node_id="g", start=2)
    assert [r[TRACE_ID_KEY] for r in a] == ["run7:g:0", "run7:g:1"]
    assert [r[TRACE_ID_KEY] for r in b] == ["run7:g:2", "run7:g:3"]   # 不与首批 0/1 碰撞


async def test_inputless_cross_batch_trace_unique(session_factory, monkeypatch):
    """跨批生成的行 trace_id 必须唯一（修：每批 attach_root_trace 从 0 起会让不同批同序行 trace 碰撞）。"""
    seq = {"n": 0}

    def fn(user):
        if user.startswith("判:"):
            seq["n"] += 1
            ok = seq["n"] % 2 == 1
            return (_json.dumps({"status": "pass" if ok else "failed", "reason": "x"}),
                    {"prompt_tokens": 1, "completion_tokens": 1})
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    run_id, _ = await make_gen_run(session_factory, GEN_QC)   # count=4、拒一半 → 必跑多批
    await run_it(session_factory, run_id)
    from app.services.trace import TRACE_ID_KEY
    gen = await runner._node_outputs(session_factory, run_id, "g", include_trace=True)
    tids = [r[TRACE_ID_KEY] for r in gen]
    assert len(gen) > 4 and len(set(tids)) == len(tids)      # 生成多批且 trace 全唯一


async def test_inputless_cancel_during_qc_stops_clean(session_factory, monkeypatch):
    """质检批在途被取消：run=cancelled、不抛未捕获 CancelledError、节点不卡 running。"""
    started, cancel = asyncio.Event(), asyncio.Event()

    async def fake(mc, system, user, params=None, retries=3):
        if user.startswith("判:"):
            started.set()
            await asyncio.Event().wait()                     # 永久阻塞，只有硬中断能解开
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    run_id, _ = await make_gen_run(session_factory, GEN_QC)
    task = asyncio.create_task(
        runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel))
    await asyncio.wait_for(started.wait(), timeout=3)         # 质检判定已在途
    cancel.set()
    await asyncio.wait_for(task, timeout=3)                   # 必须迅速结束(被中止)，不卡死/不抛
    run = await get_run(session_factory, run_id)
    assert run.status == "cancelled"
    async with session_factory() as s:
        from app.models import RunNodeState
        states = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id))).scalars().all()
    assert all(st.status != "running" for st in states)


# ---- 审查修复回归 ----

async def test_stray_gen_node_in_input_workflow_completes(session_factory, monkeypatch):
    """回归：正常 input 工作流里有一个游离(无边)的 llm_synth 节点，不得整 run failed——按旧行为 0 行 no-op 跳过。"""
    from app.models import Dataset, DatasetRow
    patch_chat(monkeypatch)
    uid, mc_id = await _mk_user_model(session_factory)
    async with session_factory() as s:
        ds = Dataset(user_id=uid, name="d", row_count=2)
        s.add(ds); await s.flush()
        for i in range(2):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=_json.dumps({"q": f"问{i}"})))
        await s.commit(); ds_id = ds.id
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "g", "type": "llm_synth",
             "config": {"model_config_id": mc_id, "user_prompt": "Q:{{q}}", "output_column": "a", "retries": 1}},
            {"id": "o", "type": "output", "config": {}},
            {"id": "stray", "type": "llm_synth", "config": {"model_config_id": mc_id, "user_prompt": "x"}},
        ],
        "edges": [{"source": "in", "target": "g", "kind": "normal"},
                  {"source": "g", "target": "o", "kind": "normal"}],
    }
    async with session_factory() as s:
        wf = Workflow(user_id=uid, name="wf", graph_json=_json.dumps(graph))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=_json.dumps(graph))
        s.add(ver); await s.flush()
        run = Run(user_id=uid, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run); await s.commit(); run_id = run.id
    await run_it(session_factory, run_id)
    assert (await get_run(session_factory, run_id)).status == "completed"   # 不因游离节点 failed
    assert len(await runner._node_outputs(session_factory, run_id, "o")) == 2


async def test_inputless_qc_col_collision_fails_run(session_factory, monkeypatch):
    """生成链 qc 状态列撞生成列 → 整 run failed 点名(与单遍一致，不静默覆盖用户生成数据)。"""
    patch_chat(monkeypatch, lambda u: (_json.dumps({"status": "pass"}) if u.startswith("判:")
                                       else "v", {"prompt_tokens": 1, "completion_tokens": 1}))
    graph = _json.loads(_json.dumps(GEN_QC))
    graph["nodes"][0]["config"]["output_column"] = "qc_status"   # 生成列撞 qc 默认 status_column
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed" and "qc_status" in (run.error or "") and "q" in (run.error or "")


async def test_inputless_batch_capped(session_factory, monkeypatch):
    """单批种子有绝对上限 _GEN_BATCH_CAP：大 count 不一次性建满，分多批逼近(防 OOM)。"""
    monkeypatch.setattr(runner, "_GEN_BATCH_CAP", 2)
    sizes = []
    real = runner.attach_root_trace

    def spy(rows, **kw):
        sizes.append(len(rows))
        return real(rows, **kw)

    monkeypatch.setattr(runner, "attach_root_trace", spy)
    patch_chat(monkeypatch)
    graph = _json.loads(_json.dumps(SYNTH_OUT))
    graph["nodes"][1]["config"]["count"] = 5
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "o")) == 5   # 仍精确达 count
    assert sizes and max(sizes) <= 2 and len(sizes) >= 3                        # 每批 ≤cap、确实分了多批


async def test_inputless_progress_never_exceeds_total(session_factory, monkeypatch):
    """生成循环跑动中 start 节点不得 done>total(进度条>100%)：total 随批次累加(修审查 Important)。"""
    from app import events
    seen = []
    orig = events.publish

    def cap(user_id, channel, *a, **kw):
        if kw.get("kind") == "progress":
            d = kw.get("data", {})
            seen.append((d.get("node_id"), d.get("done"), d.get("total")))
        return orig(user_id, channel, *a, **kw)

    monkeypatch.setattr(events, "publish", cap)
    monkeypatch.setattr(runner, "publish", cap)
    seq = {"n": 0}

    def fn(user):
        if user.startswith("判:"):
            seq["n"] += 1
            ok = seq["n"] % 2 == 1
            return (_json.dumps({"status": "pass" if ok else "failed", "reason": "x"}),
                    {"prompt_tokens": 1, "completion_tokens": 1})
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    graph = _json.loads(_json.dumps(GEN_QC))
    graph["nodes"][2]["config"]["count"] = 6
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    g_events = [(d, t) for nid, d, t in seen if nid == "g" and d is not None and t is not None]
    assert g_events and all(d <= t for d, t in g_events)   # start 节点全程 done≤total


async def test_inputless_resume_output_not_doubled(session_factory, monkeypatch):
    """生成链已达 count 后再执行(resume)：output 恒写 row_idx=0 幂等跳过，不得翻倍成 2×count。"""
    patch_chat(monkeypatch)
    run_id, _ = await make_gen_run(session_factory, SYNTH_OUT)   # synth→output(count=4)
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "o")) == 4
    async with session_factory() as s:                          # 模拟 resume：不清任何行，run 复位重跑
        run = await s.get(Run, run_id); run.status = "queued"; await s.commit()
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "o")) == 4   # 仍 4，不是 8
