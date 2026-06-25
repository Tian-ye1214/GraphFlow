# 无输入起始节点 + 生成到达 count 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `llm_synth` / `http_fetch` 节点可作工作流起点（无 input 节点）：起始节点持续分批生成数据，流过链路（含质检），输出节点累计被接收的好行，达到输出节点配置的 `count` 即停止；不设预算上限，由用户手动取消。

**Architecture:** 引擎 `_execute` 在校验后检测「无输入生成链」（存在无普通入边的生成节点）。命中则走**新增循环路径** `_run_generation_loop`，否则走现有单遍 topo（**字节级不变**）。循环每轮生成一批空种子 → 复用现有 `_run_per_row_node`（起始）/抽取出的 `_qc_judge_batch`（质检）/`_run_barrier_node`（auto_process）逐节点处理本批、按 row_idx 偏移落库不撞键 → 读回本批产出喂下个节点 → 累积「到达输出前」的接收行；达 `count` 后输出节点跑一次截到 count。仅支持单一线性链（V1）。

**Tech Stack:** Python 3 / asyncio / SQLAlchemy async / FastAPI（后端）；pytest（`backend/tests`，`session_factory`/`monkeypatch` fixtures）。前端 React + antd（仅补一处提示）。

## Global Constraints

- KISS：最简实现，不加投机抽象、不写防御未发生 bug 的代码（见记忆 [[kiss-no-defensive-code]]）。
- 复用优先、单点化，不堆补丁式新代码，死代码即清（[[graphflow-reuse-over-patch]]）。
- 绝不引入任何 dry_run / 试跑 / mock_run「假运行」（[[graphflow-no-fake-run]]、[[graphflow-batch2-dryrun-done]]）。
- 思考参数策略不动：节点所有模型参数用户可配（默认开/high/65536），不删思考、默认别降（[[graphflow-thinking-param-policy]]）。
- 全程中文注释/回复；git 提交信息不出现 claude、不加 Co-Authored-By（[[no-claude-in-git]]）。
- 测试只跑本地、不推 origin（origin 不含 backend/tests，[[graphflow-tests-not-on-origin]]）。
- 后端测试命令：`cd backend && python -m pytest tests/ -q`（单测：`python -m pytest tests/test_xxx.py::test_name -q`）。
- 新代码不破坏现有 `backend/tests/test_runner.py` 等全部用例（单遍 topo 路径行为不变）。
- 本计划全部改动落在分支 `feature/inputless-generation-start`（已存在），合并后线上需重启生效。

---

## 文件结构

- 修改：`backend/app/engine/runner.py` —— 主战场。新增 `_gen_batch_size`、`_generation_chain`、
  `_batch_outputs`、`_qc_judge_batch`、`_run_generation_loop`、`_finalize_chain_states`；给
  `_run_per_row_node` 加 `row_base`、`_run_barrier_node` 加 `row_idx`；`_run_qc_node` 改用
  `_qc_judge_batch`；`_execute` 加生成链分支。
- 新增/修改测试：`backend/tests/test_gen_loop.py`（本特性全部用例，复用 test_runner 的
  `make_run`/`patch_chat`/`run_it`/`get_run` 风格——本文件内自带这些 helper 的精简副本）。
- 修改：`frontend/src/canvas/forms/NodeConfigForm.tsx` —— 输出节点表单加一行说明（无输入起始时 count 必填）。
- 新增：`backend/tools/inputless_gen_live.py` —— 真实模型活体脚本（合并后人工跑，建即删回基线）。

---

## Task 1: 批量大小纯函数 `_gen_batch_size`

**Files:**
- Modify: `backend/app/engine/runner.py`（新增模块级函数，放在 `_resolve_output_count` 之后）
- Test: `backend/tests/test_gen_loop.py`

**Interfaces:**
- Produces: `_gen_batch_size(gap: int, fanout: int, accepted: int, generated: int) -> int`
  —— 返回本批要生成的种子行数（≥1）。`yield = accepted/generated`（generated=0 时按 1.0），
  钳到 [0.2, 1.0]；`return max(1, ceil(gap / fanout / yield_clamped))`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_gen_loop.py`，写入：

```python
import math

from app.engine.runner import _gen_batch_size


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_loop.py -q`
Expected: FAIL（`ImportError: cannot import name '_gen_batch_size'`）

- [ ] **Step 3: 实现**

在 `backend/app/engine/runner.py` 的 `_resolve_output_count` 函数之后加入：

```python
def _gen_batch_size(gap: int, fanout: int, accepted: int, generated: int) -> int:
    """无输入生成循环：本批生成多少种子行。按缺口 gap、扇出、已观测通过率(接收/已生成候选)估算——
    产率越低本批越大，缺口越小越收敛。yield 钳到 [0.2,1.0]：上界防首批过量、下界防极低产率单批暴量(≤5×缺口)。
    至少 1 行，防 ceil(gap/fanout) 因扇出大而归零导致死循环。"""
    y = (accepted / generated) if generated > 0 else 1.0
    y = min(1.0, max(0.2, y))
    return max(1, -(-gap // fanout) if y >= 1.0 else math.ceil(gap / fanout / y))
```

在文件顶部 import 区加入 `import math`（若已存在则跳过；当前 runner.py 顶部只有 `import asyncio` / `import json`，需新增）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_gen_loop.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_gen_loop.py
git commit -m "feat(engine): 无输入生成循环批量大小纯函数 _gen_batch_size" --no-gpg-sign
```

---

## Task 2: 生成链检测/校验纯函数 `_generation_chain`

**Files:**
- Modify: `backend/app/engine/runner.py`（新增模块级函数，放在 `_gen_batch_size` 之后）
- Test: `backend/tests/test_gen_loop.py`

**Interfaces:**
- Consumes: `from app.engine.graph import Graph, Node, parse_graph, upstream_ids`（runner 已 import 这些）。
- Produces: `_generation_chain(graph: Graph) -> list[Node] | None`
  - 无「无普通入边的生成节点」→ 返回 `None`（普通图，走单遍 topo）。
  - 有 → 校验整图为单一线性链 `start →…→ output(count≥1)`；合法返回有序链 `[start, …, output]`，
    非法 `raise ValueError(中文点名)`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_gen_loop.py` 追加：

```python
import pytest

from app.engine.graph import parse_graph
from app.engine.runner import _generation_chain

_SYN = {"id": "g", "type": "llm_synth", "config": {}}
_E = lambda a, b: {"source": a, "target": b, "kind": "normal"}  # noqa: E731


def _chain(nodes, edges):
    return _generation_chain(parse_graph({"nodes": nodes, "edges": edges}))


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


def test_generation_chain_rejects_mixed_input_node():
    nodes = [{"id": "in", "type": "input", "config": {}}, _SYN,
             {"id": "h", "type": "http_fetch", "config": {}},
             {"id": "o", "type": "output", "config": {"count": 5}}]
    # h 无父(无输入生成节点)，但图里还有 input 节点 → 混用报错
    with pytest.raises(ValueError, match="input"):
        _chain(nodes, [_E("in", "g"), _E("g", "o"), _E("h", "o")])


def test_generation_chain_rejects_stray_node():
    nodes = [_SYN, {"id": "o", "type": "output", "config": {"count": 5}},
             {"id": "stray", "type": "llm_synth", "config": {}}]
    # stray 也无父 → 两个起始节点 → 报错
    with pytest.raises(ValueError, match="一个起始"):
        _chain(nodes, [_E("g", "o")])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_loop.py -q`
Expected: FAIL（`ImportError: cannot import name '_generation_chain'`）

- [ ] **Step 3: 实现**

在 `backend/app/engine/runner.py` 的 `_gen_batch_size` 之后加入：

```python
def _generation_chain(graph: Graph) -> list[Node] | None:
    """无输入生成链检测/校验。无「无普通入边的生成节点」→ None(走单遍 topo)。
    有 → 要求整图是单一线性链 start(llm/http)→…→output(count≥1)：否则点名 ValueError(整 run failed)。"""
    gen_types = {"llm_synth", "http_fetch"}
    starts = [n for n in graph.nodes if n.type in gen_types and not upstream_ids(graph, n.id)]
    if not starts:
        return None
    if any(n.type == "input" for n in graph.nodes):
        raise ValueError("无输入起始的生成链不能与 input 节点混用（请删去 input 节点或为生成节点接入上游）")
    if len(starts) > 1:
        raise ValueError("无输入起始的生成链只能有一个起始节点")
    normal = [e for e in graph.edges if e["kind"] == "normal"]
    by_id = {n.id: n for n in graph.nodes}
    chain: list[Node] = []
    seen: set[str] = set()
    cur = starts[0]
    while True:
        chain.append(cur)
        seen.add(cur.id)
        children = [e["target"] for e in normal if e["source"] == cur.id]
        if cur.type == "output":
            if children:
                raise ValueError(f"无输入生成链的输出节点 {cur.id} 不能有下游")
            break
        if len(children) != 1:
            raise ValueError(f"无输入生成链必须是单链：节点 {cur.id} 的下游不是恰好一个")
        child = by_id[children[0]]
        if len([e for e in normal if e["target"] == child.id]) != 1:
            raise ValueError(f"无输入生成链必须是单链：节点 {child.id} 有多个上游（不支持合并）")
        if child.id in seen:
            raise ValueError("无输入生成链不能包含环")
        cur = child
    out = chain[-1]
    if out.type != "output":
        raise ValueError("无输入生成链必须以输出节点结尾")
    if len(chain) != len(graph.nodes):
        raise ValueError("无输入生成链含未连入主链的游离节点")
    count = out.config.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"无输入起始的生成链必须在输出节点设置接收数量（count≥1），当前为 {count!r}")
    return chain
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_gen_loop.py -q`
Expected: PASS（全部通过）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_gen_loop.py
git commit -m "feat(engine): 无输入生成链检测/校验 _generation_chain（单链+count必填）" --no-gpg-sign
```

---

## Task 3: 给 `_run_per_row_node` 加 `row_base`、`_run_barrier_node` 加 `row_idx` + `_batch_outputs`

让逐行节点与 barrier 节点能在指定 row_idx 偏移/槽位落库（供循环按批不撞 `(run_id,node_id,row_idx)` 唯一键），
并能按区间读回本批产出。默认参数 0 → 现有单遍路径行为不变。

**Files:**
- Modify: `backend/app/engine/runner.py`
- Test: `backend/tests/test_gen_loop.py`

**Interfaces:**
- Produces:
  - `_run_per_row_node(..., *, row_coro, log_source=None, max_output_rows=None, row_base: int = 0)`
    —— 写第 `row_base+i` 行；resume 的 done/failed 判定按绝对 row_idx。
  - `_run_barrier_node(session_factory, run_id, user_id, node, inputs, *, row_idx: int = 0)`
  - `_batch_outputs(session_factory, run_id, node_id, lo: int, hi: int) -> list[dict]`
    —— 返回 `lo ≤ row_idx < hi` 的 done 行展平（含 trace，供链式衔接）。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_gen_loop.py` 追加（这些 helper 也供后续 Task 复用）：

```python
import asyncio
import json as _json

from sqlalchemy import select

from app import crypto
from app.engine import runner
from app.models import ModelConfig, Run, RunRow, User, Workflow, WorkflowVersion
from app.services import llm


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_loop.py::test_per_row_node_row_base_writes_offset -q`
Expected: FAIL（`_run_per_row_node() got an unexpected keyword argument 'row_base'`）

- [ ] **Step 3: 实现**

在 `backend/app/engine/runner.py`：

(a) `_run_per_row_node` 签名加 `row_base: int = 0`，函数体把 row_idx 与输入下标分离：

```python
async def _run_per_row_node(session_factory, run_id, user_id, node: Node, inputs, cancel_event,
                            *, row_coro, log_source=None, max_output_rows=None, row_base: int = 0):
```

把 todo 计算、work 写库、trace 改为：

```python
    todo = [i for i in range(len(inputs))
            if (row_base + i) not in done_idx and (row_base + i) not in failed_idx]
```

并把内层 `work(idx)` 改名语义为「输入下标 i」，写库用 `row_base + i`：

```python
    async def work(i: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            trace_id = row_trace_id(inputs[i])
            ctx = (log_context(run_id=run_id, node_id=node.id, user_id=user_id,
                               source=log_source, trace_id=trace_id)
                   if log_source else nullcontext())
            try:
                with ctx:
                    out_rows, usage = await _cancellable(row_coro(i), cancel_event)
            except asyncio.CancelledError:
                return
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, row_base + i, "failed", [], str(e),
                                  trace_id=trace_id)
                failed_count += 1
            else:
                out_rows = attach_child_trace(inputs[i], out_rows, node_id=node.id, row_idx=row_base + i)
                await _write_unit(session_factory, run_id, node.id, row_base + i, "done", out_rows, "",
                                  usage=usage, drop=cfg.get("drop_columns"))
                done_count += 1
            await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
```

（其余行不变。`done_idx`/`failed_idx` 仍是「已存在 RunRow 的绝对 row_idx 集合」，与 `row_base+i` 同空间比较，正确。）

(b) `_run_barrier_node` 签名加 `row_idx: int = 0`，把两处硬编码 `0` 换成 `row_idx`：

```python
async def _run_barrier_node(session_factory, run_id, user_id, node: Node, inputs, *, row_idx: int = 0):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == row_idx
        ))).scalar_one_or_none()
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done", total=1, done=1, failed=0)
        return
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running", total=1, done=0, failed=0)
    try:
        out = await _barrier_output(session_factory, user_id, node, inputs, run_id=run_id)
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, row_idx, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="failed", total=1, done=0, failed=1)
        raise
    await _write_unit(session_factory, run_id, node.id, row_idx, "done", out, "",
                      drop=node.config.get("drop_columns"))
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done", total=1, done=1, failed=0)
```

(c) 新增 `_batch_outputs`，放在 `_node_outputs` 之后：

```python
async def _batch_outputs(session_factory, run_id, node_id, lo: int, hi: int) -> list[dict]:
    """读回某节点 row_idx ∈ [lo, hi) 的 done 行并展平（含 trace，用于生成循环链式衔接）。"""
    async with session_factory() as s:
        recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.status == "done",
            RunRow.row_idx >= lo, RunRow.row_idx < hi).order_by(RunRow.row_idx))).scalars().all()
    out: list[dict] = []
    for r in recs:
        out.extend(json.loads(r.data_json))
    return out
```

- [ ] **Step 4: 跑新测试 + 全量回归**

Run: `cd backend && python -m pytest tests/test_gen_loop.py::test_per_row_node_row_base_writes_offset tests/test_runner.py -q`
Expected: PASS（新测试过 + test_runner 全绿，证明默认 row_base/row_idx 路径不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_gen_loop.py
git commit -m "feat(engine): 逐行/barrier 节点支持 row 偏移落库 + _batch_outputs 区间读回" --no-gpg-sign
```

---

## Task 4: 抽取质检判定核心 `_qc_judge_batch`

把 `_run_qc_node` 内联的 `judge_all` 抽成模块级单点，供单遍 QC 与生成循环共用（DRY）。纯重构，QC 行为不变。

**Files:**
- Modify: `backend/app/engine/runner.py`
- Test: 现有 `backend/tests/test_runner.py`（QC 相关）+ `backend/tests/test_qc*.py` 必须仍全绿。

**Interfaces:**
- Produces: `_qc_judge_batch(session_factory, run_id, user_id, node, rows, jmcs, pass_k, status_col, feedback_col, user_sem, cancel_event) -> tuple[list[dict], list[dict], dict, int]`
  —— 一轮判定：返回 `(passed, failed, usage, first_round_pass)`。`passed` 行已加 `status_col=pass`/`feedback_col=""`
  且 strip 掉 `_qc_*` 内部键；`failed` 行带 `status_col=failed`/`feedback_col=reason`/`_qc_reason`/`_qc_per_model`。

- [ ] **Step 1: 实现抽取（纯重构，无新测试，靠现有 QC 用例守护）**

在 `backend/app/engine/runner.py` 的 `_run_qc_node` **之前**新增：

```python
async def _qc_judge_batch(session_factory, run_id, user_id, node: Node, rows, jmcs, pass_k,
                          status_col, feedback_col, user_sem, cancel_event):
    """一轮 K-of-N 质检判定（单点，供单遍 QC 与无输入生成循环共用）。
    返回 (通过行, 不通过行, usage 汇总, 通过数)。逐行隔离、硬中断沿用。"""
    cfg = node.config
    sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def judge(row):
        async with sem:
            with log_context(run_id=run_id, node_id=node.id, user_id=user_id,
                             source="qc", trace_id=row_trace_id(row)):
                return await _cancellable(
                    nodes.run_qc_judge_row(cfg, row, jmcs, pass_k, user_sem), cancel_event)

    usage = nodes.zero_usage()
    passed, failed = [], []
    for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
        nodes.add_usage(usage, u)
        if ok:
            passed.append({**nodes.strip_qc_internal(row), status_col: "pass", feedback_col: ""})
        else:
            failed.append({**row, status_col: "failed", feedback_col: reason,
                           "_qc_reason": reason, "_qc_per_model": per_model})
    return passed, failed, usage, len(passed)
```

把 `_run_qc_node` 内的 `judge_all` 内嵌函数**删除**，两处调用改为调用单点。具体：

将原本：

```python
    sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def judge_all(rows):
        async def judge(row):
            async with sem:
                with log_context(run_id=run_id, node_id=node.id, user_id=user_id,
                                 source="qc", trace_id=row_trace_id(row)):
                    return await _cancellable(
                        nodes.run_qc_judge_row(cfg, row, jmcs, pass_k, user_sem), cancel_event)
        passed_, failed_ = [], []
        for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append({**nodes.strip_qc_internal(row), status_col: "pass", feedback_col: ""})
            else:
                failed_.append({**row, status_col: "failed", feedback_col: reason,
                                "_qc_reason": reason, "_qc_per_model": per_model})
        return passed_, failed_

    try:
        passed, failed = await judge_all(inputs)
```

替换为：

```python
    async def judge_all(rows):
        passed_, failed_, u, _ = await _qc_judge_batch(
            session_factory, run_id, user_id, node, rows, jmcs, pass_k,
            status_col, feedback_col, user_sem, cancel_event)
        fold(u)
        return passed_, failed_

    try:
        passed, failed = await judge_all(inputs)
```

（保留 `judge_all` 薄包装让回扫循环 `fresh_pass, failed = await judge_all(regenerated)` 不变；`fold`/`usage` 闭包仍负责累计 usage。删掉原先重复的 `sem = ...` 那一行——sem 现在在 `_qc_judge_batch` 内部。）

- [ ] **Step 2: 跑 QC 全量回归**

Run: `cd backend && python -m pytest tests/test_runner.py tests/test_qc.py tests/test_qc_multi.py tests/test_qc_columns.py tests/test_qc_adversarial.py tests/test_models_qc.py -q`
Expected: PASS（QC 行为/usage 计数不变）

- [ ] **Step 3: 提交**

```bash
git add backend/app/engine/runner.py
git commit -m "refactor(engine): 抽取质检判定核心 _qc_judge_batch（单遍QC与生成循环共用）" --no-gpg-sign
```

---

## Task 5: 生成循环主体（无质检）+ 接入 `_execute`

实现 `_run_generation_loop` 的无质检路径（`start(llm/http)→output(count)`）并接入 `_execute`：检测到生成链就走循环。

**Files:**
- Modify: `backend/app/engine/runner.py`
- Test: `backend/tests/test_gen_loop.py`

**Interfaces:**
- Consumes: `_generation_chain`、`_gen_batch_size`、`_batch_outputs`、`_run_per_row_node(row_base=)`、
  `_run_barrier_node`、`validate_node_config_shape`、`attach_root_trace`、`_node_outputs`、`_set_node_state`。
- Produces: `_run_generation_loop(session_factory, run_id, user_id, graph, chain, user_sem, cancel_event) -> None`
  以及 `_finalize_chain_states(session_factory, run_id, user_id, nodes_) -> None`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_gen_loop.py` 追加：

```python
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
        o = (await s.execute(select(runner.RunNodeState).where(
            runner.RunNodeState.run_id == run_id, runner.RunNodeState.node_id == "o"))).scalar_one()
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


async def test_inputless_http_generates_count(session_factory, monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, _json.dumps({"v": "ok"})
    from app.services import http
    monkeypatch.setattr(http, "fetch", fake_fetch)
    graph = {
        "nodes": [
            {"id": "h", "type": "http_fetch",
             "config": {"endpoint": "http://x/api", "extract": {"v": "v"}, "concurrency": 4}},
            {"id": "o", "type": "output", "config": {"count": 3}},
        ],
        "edges": [{"source": "h", "target": "o", "kind": "normal"}],
    }
    run_id, _ = await make_gen_run(session_factory, graph)
    await run_it(session_factory, run_id)
    out = await runner._node_outputs(session_factory, run_id, "o")
    assert len(out) == 3 and all(r["v"] == "ok" for r in out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_loop.py::test_inputless_synth_generates_exact_count -q`
Expected: FAIL（run.status 不是 completed 或 out 为 0 行——当前无生成循环）

- [ ] **Step 3: 实现**

(a) 在 `backend/app/engine/runner.py` 新增 `_finalize_chain_states` 与 `_run_generation_loop`（放在 `_run_qc_node` 之后）：

```python
async def _finalize_chain_states(session_factory, run_id, user_id, nodes_) -> None:
    """生成循环收尾：按落库 RunRow 给链上各节点补写累计状态（逐行节点=成功/失败行数；barrier/qc=展平行数）。"""
    for n in nodes_:
        flat = await _node_outputs(session_factory, run_id, n.id)
        async with session_factory() as s:
            failed = (await s.execute(select(func.count()).select_from(RunRow).where(
                RunRow.run_id == run_id, RunRow.node_id == n.id, RunRow.status == "failed"))).scalar()
        await _set_node_state(session_factory, run_id, n.id, user_id=user_id, status="done",
                              total=len(flat) + failed, done=len(flat), failed=failed)


async def _run_generation_loop(session_factory, run_id, user_id, graph: Graph, chain: list[Node],
                               user_sem, cancel_event) -> None:
    """无输入生成链执行：持续分批生成空种子→流过链路→累积「到达输出前」的接收行，达 output.count 即停。
    不设预算上限；cancel_event 置位即停（已接收行保留，输出未写）。仅在 _execute 确认是生成链时调用。"""
    start, output = chain[0], chain[-1]
    middle = chain[1:-1]                               # auto_process / qc（Task 6/7 接入）
    target = output.config["count"]
    validate_node_config_shape(start)                 # fanout_n 等配置错误→整 run failed 点名
    mc = None
    if start.type == "llm_synth":
        async with session_factory() as s:
            mc = await s.get(ModelConfig, start.config.get("model_config_id"))
        if mc is None or mc.user_id != user_id:
            raise ValueError(f"节点 {start.id}: 模型配置不存在")
    fanout = start.config.get("fanout_n", 1) if start.type == "llm_synth" else 1
    qc_ctx = await _prepare_qc_nodes(session_factory, user_id, graph, middle)   # Task 6 提供；无 qc 时返回 {}

    # 断点续跑：各节点行号游标 = 已落 RunRow 最大 row_idx+1；已接收 = 输出前末节点已落 done 行
    written: dict[str, int] = {}
    async with session_factory() as s:
        for n in chain:
            mx = (await s.execute(select(func.max(RunRow.row_idx)).where(
                RunRow.run_id == run_id, RunRow.node_id == n.id))).scalar()
            written[n.id] = (mx + 1) if mx is not None else 0
    last_pre = middle[-1] if middle else start
    accepted = await _node_outputs(session_factory, run_id, last_pre.id, include_trace=True)
    generated = len(await _node_outputs(session_factory, run_id, start.id, include_trace=True))

    while len(accepted) < target and not cancel_event.is_set():
        gap = target - len(accepted)
        batch_len = _gen_batch_size(gap, fanout, len(accepted), generated)
        seeds = attach_root_trace([{} for _ in range(batch_len)], run_id=run_id, node_id=start.id)
        base = written[start.id]
        if start.type == "llm_synth":
            await _run_per_row_node(
                session_factory, run_id, user_id, start, seeds, cancel_event,
                row_coro=lambda i, rs=seeds: nodes.run_llm_synth_row(start.config, rs[i], mc, user_sem),
                log_source="synth", row_base=base)
        else:
            await _run_per_row_node(
                session_factory, run_id, user_id, start, seeds, cancel_event,
                row_coro=lambda i, rs=seeds: nodes.run_http_fetch_row(start.config, rs[i]),
                row_base=base)
        rows = await _batch_outputs(session_factory, run_id, start.id, base, base + len(seeds))
        written[start.id] += len(seeds)
        generated += len(rows)
        for node in middle:                            # Task 6/7：逐个中间节点处理本批
            if cancel_event.is_set():
                return
            rows = await _run_chain_middle(session_factory, run_id, user_id, node,
                                           rows, written[node.id], qc_ctx, user_sem, cancel_event)
            written[node.id] += 1
        accepted.extend(rows)
        await _set_node_state(session_factory, run_id, output.id, user_id=user_id, status="running",
                              total=target, done=min(len(accepted), target), failed=0)

    if cancel_event.is_set():
        return
    await _run_barrier_node(session_factory, run_id, user_id, output,
                            accepted[:target], row_idx=written[output.id])
    await _finalize_chain_states(session_factory, run_id, user_id, chain[:-1])
    await _set_node_state(session_factory, run_id, output.id, user_id=user_id, status="done",
                          total=target, done=min(len(accepted), target), failed=0)
```

为让本任务（无 qc）可独立通过，先加最小占位的 `_prepare_qc_nodes` 与 `_run_chain_middle`（Task 6 再扩展 qc 分支）：

```python
async def _prepare_qc_nodes(session_factory, user_id, graph: Graph, middle) -> dict:
    """预解析链上各 qc 节点的判定模型/pass_k/列名（Task 6 起用）。无 qc 时返回空。"""
    return {}


async def _run_chain_middle(session_factory, run_id, user_id, node: Node, rows, row_idx,
                            qc_ctx, user_sem, cancel_event) -> list[dict]:
    """生成循环中间节点（本批）：auto_process 走 barrier，qc 走判定（Task 6）。返回本批产出行。"""
    if node.type == "auto_process":
        await _run_barrier_node(session_factory, run_id, user_id, node, rows, row_idx=row_idx)
        return await _batch_outputs(session_factory, run_id, node.id, row_idx, row_idx + 1)
    raise ValueError(f"无输入生成链暂不支持中间节点类型: {node.type}")
```

(b) 接入 `_execute`：在 `validate_graph(graph)` 之后、`_resolve_prompt_refs` 之后、topo 循环之前加分支。把 `_execute` 改为：

```python
async def _execute(run_id, session_factory, user_sem, cancel_event):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        run.status = "running"
        run.started_at = run.started_at or _now()
        await s.commit()
        user_id = run.user_id
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)
    await _resolve_prompt_refs(session_factory, graph, user_id)
    await _log(session_factory, run_id, "", "运行开始")

    chain = _generation_chain(graph)                  # 无输入生成链？否则 None 走单遍 topo
    if chain is not None:
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
        await _run_generation_loop(session_factory, run_id, user_id, graph, chain, user_sem, cancel_event)
        return await _finish(session_factory, run_id,
                             "cancelled" if cancel_event.is_set() else "completed")

    for node in topo_order(graph):
        ...（原循环体保持不变）
```

（`_generation_chain` 抛 ValueError 时由 `execute_run` 捕获落 run.failed 点名——与现有错误路径一致。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && python -m pytest tests/test_gen_loop.py tests/test_runner.py -q`
Expected: PASS（三个新生成用例过 + test_runner 全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_gen_loop.py
git commit -m "feat(engine): 无输入生成循环主体(无质检)+接入_execute，达count即停" --no-gpg-sign
```

---

## Task 6: 生成循环接入质检（生成 > count、接收 = count）

**Files:**
- Modify: `backend/app/engine/runner.py`（扩展 `_prepare_qc_nodes` / `_run_chain_middle`）
- Test: `backend/tests/test_gen_loop.py`

**Interfaces:**
- Consumes: `_qc_judge_batch`、`_write_unit`、`QcMetric`。
- Produces: 扩展后的 `_prepare_qc_nodes(...) -> dict[node_id, dict]`（每 qc 节点：jmcs/pass_k/status_col/feedback_col），
  `_run_chain_middle` 支持 `node.type == "qc"`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_gen_loop.py` 追加：

```python
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
        metrics = (await s.execute(select(runner.QcMetric).where(
            runner.QcMetric.run_id == run_id))).scalars().all()
    assert sum(m.total for m in metrics) == len(gen)        # QcMetric 跨批聚合=总判定数
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_loop.py::test_inputless_gen_with_qc_reaches_count -q`
Expected: FAIL（`无输入生成链暂不支持中间节点类型: qc`）

- [ ] **Step 3: 实现**

把 `_prepare_qc_nodes` 实现为（替换 Task 5 的占位）：

```python
async def _prepare_qc_nodes(session_factory, user_id, graph: Graph, middle) -> dict:
    """预解析链上各 qc 节点的判定模型/pass_k(钳)/列名（生成循环每批复用，避免反复查库）。"""
    ctx: dict[str, dict] = {}
    async with session_factory() as s:
        for node in middle:
            if node.type != "qc":
                continue
            cfg = node.config
            judge_ids = cfg.get("judge_model_ids") or (
                [cfg["model_config_id"]] if cfg.get("model_config_id") else [])
            jmcs = [await s.get(ModelConfig, jid) for jid in judge_ids]
            if not jmcs or any(m is None or m.user_id != user_id for m in jmcs):
                raise ValueError(f"质检节点 {node.id}: 判定模型配置不存在")
            try:
                pass_k = int(cfg.get("pass_k", 1))
            except (TypeError, ValueError):
                pass_k = 1
            ctx[node.id] = {
                "jmcs": jmcs, "pass_k": max(1, min(pass_k, len(jmcs))),
                "status_col": cfg.get("status_column") or "qc_status",
                "feedback_col": cfg.get("feedback_column") or "qc_feedback"}
    return ctx
```

把 `_run_chain_middle` 扩展 qc 分支：

```python
async def _run_chain_middle(session_factory, run_id, user_id, node: Node, rows, row_idx,
                            qc_ctx, user_sem, cancel_event) -> list[dict]:
    """生成循环中间节点（本批）：auto_process→barrier，qc→判定一轮(通过累积、不通过淘汰由外层循环补生成)。
    返回本批产出行。qc 本批通过行落 RunRow(供 readback/观测)，首轮通过计入 QcMetric(first_round_rate 聚合)。"""
    if node.type == "auto_process":
        await _run_barrier_node(session_factory, run_id, user_id, node, rows, row_idx=row_idx)
        return await _batch_outputs(session_factory, run_id, node.id, row_idx, row_idx + 1)
    if node.type == "qc":
        c = qc_ctx[node.id]
        passed, _failed, usage, first_pass = await _qc_judge_batch(
            session_factory, run_id, user_id, node, rows, c["jmcs"], c["pass_k"],
            c["status_col"], c["feedback_col"], user_sem, cancel_event)
        await _write_unit(session_factory, run_id, node.id, row_idx, "done", passed, "",
                          usage=usage, drop=node.config.get("drop_columns"))
        async with session_factory() as s:
            s.add(QcMetric(run_id=run_id, node_id=node.id, total=len(rows), first_round_pass=first_pass))
            await s.commit()
        return await _batch_outputs(session_factory, run_id, node.id, row_idx, row_idx + 1)
    raise ValueError(f"无输入生成链暂不支持中间节点类型: {node.type}")
```

注：生成循环里的 qc 是**判定一轮**（不走回扫——外层循环自身负责补生成补足 count）；不通过样本不落 `QcFailure`
（淘汰即重生成的过程产物，避免无界膨胀；观测靠 QcMetric 通过率）。这是与单遍 QC 节点的有意差异。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && python -m pytest tests/test_gen_loop.py tests/test_runner.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_gen_loop.py
git commit -m "feat(engine): 生成循环接入质检——淘汰即补生成，接收达count即停" --no-gpg-sign
```

---

## Task 7: 取消、低产率收敛、auto_process 链、累计状态

**Files:**
- Modify: 无（功能已在 Task 5/6 实现）；本任务补测试守护。
- Test: `backend/tests/test_gen_loop.py`

- [ ] **Step 1: 写测试**

在 `backend/tests/test_gen_loop.py` 追加：

```python
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
```

- [ ] **Step 2: 跑测试**

Run: `cd backend && python -m pytest tests/test_gen_loop.py -q`
Expected: PASS（全部生成循环用例通过）

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_gen_loop.py
git commit -m "test(engine): 生成循环 低产率收敛/中途取消/auto_process链 守护" --no-gpg-sign
```

---

## Task 8: 运行期校验错误（缺 count / 非线性 / 混 input）落 run.failed 点名

**Files:**
- Modify: 无（`_generation_chain` 已抛错、`execute_run` 已捕获）；本任务补端到端测试。
- Test: `backend/tests/test_gen_loop.py`

- [ ] **Step 1: 写测试**

在 `backend/tests/test_gen_loop.py` 追加：

```python
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
```

- [ ] **Step 2: 跑测试 + 全量回归**

Run: `cd backend && python -m pytest tests/test_gen_loop.py tests/test_runner.py tests/test_graph.py -q`
Expected: PASS

- [ ] **Step 3: 全量后端回归**

Run: `cd backend && python -m pytest tests/ -q`
Expected: PASS（全绿；确认无回归）

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_gen_loop.py
git commit -m "test(engine): 无输入生成链 缺count/分叉 运行期点名failed 守护" --no-gpg-sign
```

---

## Task 9: 前端提示（无输入起始时输出 count 必填）

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（输出节点表单 `count` 字段附近加说明文案）
- Test: 无新单测（纯文案）；跑 `cd frontend && npm run build` 确认 tsc clean。

**Interfaces:**
- Consumes: 现有 `OutputForm`/输出节点表单中渲染 `count` 的 `Field`。

- [ ] **Step 1: 定位输出节点 count 字段**

Run: `cd frontend && grep -n "count" src/canvas/forms/NodeConfigForm.tsx`
找到渲染输出节点 `count` 的 `Field`（产量上限）。

- [ ] **Step 2: 在该 Field 下加说明**

在 `count` 的 `<Field label="...">...</Field>` 内、`InputNumber` 之后追加一行说明（antd `Typography.Text` 或普通 `div`）：

```tsx
<div style={{ marginTop: 4, color: '#999', fontSize: 12 }}>
  无输入起始（直接从 LLM/HTTP 节点生成）时此项必填：生成将持续到接收够 count 条为止。
</div>
```

（若该文件已有统一的小字说明组件/样式，复用之，不新造样式。）

- [ ] **Step 3: 构建确认 tsc clean**

Run: `cd frontend && npm run build`
Expected: 构建成功，无类型错误。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(web): 输出节点提示——无输入起始时 count 必填(生成到达即停)" --no-gpg-sign
```

---

## Task 10: 真实模型活体脚本（合并后人工跑）

**Files:**
- Create: `backend/tools/inputless_gen_live.py`

**Interfaces:**
- Consumes: 真实 zrs 模型配置 + 真实 DeepSeek（与现有 `backend/tools/*_live.py` 同套路：打真实 8000 端口或直连
  session_factory）。脚本必须 smoke 用户「建即删」、跑完回基线，不污染真实数据。

- [ ] **Step 1: 参照既有 live 脚本写**

先看一个既有 live 脚本的结构：
Run: `ls backend/tools/*live*.py && sed -n '1,40p' backend/tools/large_dataset_live.py`（或任一 `*_live.py`）。
照搬其「建临时用户/模型→建工作流→触发 run→轮询→断言→删临时数据回基线」骨架，构造两条无输入生成链：
① `llm_synth(zrs) → output(count=8)`；② `llm_synth → qc(zrs判定) → output(count=8)`。
断言：① output 恰 8 行；② output 恰 8 行且生成 > 8、QcMetric 通过率合理；run.status=completed。

- [ ] **Step 2: 本地真实跑（合并后、线上重启后执行）**

Run: `cd backend && python tools/inputless_gen_live.py`
Expected: 两条链均 completed、断言全过、临时数据已删回基线。
（此步在真实环境人工执行；记录结果。）

- [ ] **Step 3: 提交**

```bash
git add backend/tools/inputless_gen_live.py
git commit -m "test(live): 无输入生成链真实模型活体脚本(建即删回基线)" --no-gpg-sign
```

---

## Self-Review（计划自检）

**1. Spec 覆盖：**
- 触发与校验（单链/缺count/混input）→ Task 2 + Task 8。
- 执行循环（分批生成、自适应、复用节点函数）→ Task 1 + Task 5。
- 质检（淘汰即补、接收=count、生成>count）→ Task 6。
- 不设预算/手动取消 → Task 7（cancel 用例）。
- 进度/持久化（output accepted/count、QcMetric 聚合、按偏移落库）→ Task 5/6（状态断言）。
- 续跑（written 游标 + 读已接收）→ 已在 `_run_generation_loop` 实现（resume 读 max row_idx + last_pre 已落行）；
  未单列任务（属循环内逻辑，被 Task 5/6 端到端覆盖）。
- 范围（auto_process 链/V1 线性）→ Task 7。
- 前端提示 → Task 9。
- 真实活体 → Task 10。
- llm + http 双支持 → Task 5（http 用例）。

**2. Placeholder 扫描：** 各步含完整代码/命令/预期；Task 7 的 auto_process 算子名给了「先确认、否则替换」的明确兜底，
Task 10 给了「参照既有 live 脚本」的明确骨架来源——非占位。

**3. 类型/签名一致：**
- `_gen_batch_size(gap, fanout, accepted, generated)`、`_generation_chain(graph)->list[Node]|None`、
  `_batch_outputs(.., lo, hi)`、`_qc_judge_batch(..)->(passed,failed,usage,first_pass)`、
  `_run_generation_loop(.., chain, ..)`、`_run_chain_middle(.., node, rows, row_idx, qc_ctx, ..)`、
  `_prepare_qc_nodes(..)->dict` 全程一致。
- `_run_per_row_node` 新增 `row_base`、`_run_barrier_node` 新增 `row_idx`，默认值保持单遍路径不变。

**已知差异/限制（V1，写入设计文档亦已注明）：**
- 生成循环 qc 为判定一轮、不走回扫、不落 QcFailure（与单遍 QC 有意不同）。
- auto_process 跨批去重/shuffle 仅批内生效。
- 中间节点级状态为收尾近似（headline 为 output accepted/count）。
- 不设预算上限，长跑高拒绝率会让 RunRow 随生成量增长，靠手动取消收敛。
