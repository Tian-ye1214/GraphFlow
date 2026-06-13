# 节点 LLM 化 + 体验批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 GraphFlow 的质检节点改为纯 LLM 语义判定、给每个有提示词的节点接 RedLotus 助手，并落地 5 项体验改进（Agent 目录按用户名、数据集页置顶、画布自动保存、数据集内联预览、运行硬中断）。

**Architecture:** 后端先行——QC 判定复用 `llm.chat` + `render_template`（新增 `run_qc_judge_row`，删规则版 `qc_split`）；硬中断用一个 `_cancellable(coro, event)` 小助手把进行中的 httpx 调用立刻 abort；节点助手复用 codegen 的临时单 Agent + `gather_sample_rows`。前端随后改 4 个文件。全程 TDD、KISS、不碰 api_keys、保持按用户隔离。

**Tech Stack:** FastAPI + SQLAlchemy async + pydantic-ai 1.107（测试用 `FunctionModel`）+ pytest；React 19 + antd 6.4.3 + React Flow + vitest。后端测试 `cd backend && uv run pytest`，前端 `cd frontend && npx vitest run` + `npm run build`。

**前置已读基线（实现者无需重新探索）：**
- `backend/app/engine/nodes.py`：`render_template`、`run_llm_synth_row`（已处理 `_qc_reason` 注入与剥离、json 输出）、规则 `qc_split` + `_predicate`。
- `backend/app/engine/runner.py`：`_run_qc_node` 现为规则版；`_run_llm_node.work` 逐行；`_execute` 节点间查 `cancel_event`。
- `backend/app/services/llm.py:chat(mc, system, user, params, retries)`：`params["json_mode"]` 触发 `response_format`。
- `backend/app/agent/turns.py:session_dir(session_id)`、`codegen.py:gather_sample_rows/generate_with_repair/strip_code_fences/_user_prompt`、`factory.py:create_agent`。
- CLI `cmd_node_set` 已有键：`model`→model_config_id、`system`→system_prompt、`prompt`→user_prompt、`conc`→concurrency、`max_rounds`。规则键在 228-235 行。

---

## Task 1: Agent 工作目录按用户名命名（§2）

**Files:**
- Modify: `backend/app/agent/turns.py`（`session_dir` 签名 + `_run_turn`）
- Modify: `backend/app/routers/agent.py`（两处调用：create_session、delete_session）
- Test: `backend/tests/test_agent_turns.py`（新增一个用例）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_turns.py` 末尾追加（文件已存在，保持其原有 import 风格；若缺 import 则在顶部加 `from app.agent import turns` 与 `from app.config import settings`）：

```python
def test_session_dir_uses_sanitized_username(monkeypatch, tmp_path):
    from app.agent import turns
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    p = turns.session_dir("a/b:c", 7)
    assert p.is_absolute()
    assert p.parts[-2:] == ("a_b_c", "7")  # 用户名清洗非法字符，会话 id 作子目录
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_agent_turns.py::test_session_dir_uses_sanitized_username -q`
Expected: FAIL（`session_dir()` 现签名只收 1 个参数 → TypeError）

- [ ] **Step 3: 改 `session_dir` 签名**

`backend/app/agent/turns.py` 顶部加 `import re`，把 `session_dir` 改成：

```python
def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def session_dir(username: str, session_id: int) -> Path:
    # 必须绝对：相对路径会被 gf 子进程按其 cwd 二次拼接（GF_STATE_FILE 失效→Agent 自行 login 成幽灵用户）
    return (settings.data_dir / "agent" / _safe(username) / str(session_id)).resolve()
```

- [ ] **Step 4: `_run_turn` 传 username**

`_run_turn` 里把 `models = {...}` 那段所在的 `async with sf() as s:` 块加一行查用户名（同一会话内），并改 workdir 调用。具体：在 `sess = await s.get(AgentSession, session_id)` 之后、出块之前取得 `username`：

```python
        from app.models import User
        user = await s.get(User, user_id)
        username = user.username
```

然后把 `workdir=session_dir(session_id)` 改为 `workdir=session_dir(username, session_id)`。

- [ ] **Step 5: 改 agent.py 两处调用**

`backend/app/routers/agent.py`：
- create_session 里 `wd = session_dir(sess.id)` → `wd = session_dir(user.username, sess.id)`
- delete_session 里 `shutil.rmtree(session_dir(sid), ignore_errors=True)` → `shutil.rmtree(session_dir(user.username, sid), ignore_errors=True)`

- [ ] **Step 6: 跑测试 + 全量 agent 测试**

Run: `cd backend && uv run pytest tests/test_agent_turns.py tests/test_agent_api.py -q`
Expected: PASS（新用例过；既有用例不回归）

- [ ] **Step 7: 提交**

```bash
git add backend/app/agent/turns.py backend/app/routers/agent.py backend/tests/test_agent_turns.py
git commit -m "feat: Agent 工作目录按用户名/会话id 命名（清洗非法字符）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 运行硬中断 `_cancellable`（§7）

**Files:**
- Modify: `backend/app/engine/runner.py`（新增 `_cancellable`；改 `_run_llm_node.work`）
- Test: `backend/tests/test_runner.py`（用确定性硬中断用例替换 `test_cancel_during_llm_node`）

- [ ] **Step 1: 写失败测试（替换旧的软取消用例）**

在 `backend/tests/test_runner.py` 中**删除** `test_cancel_during_llm_node`（171? 实为 245-265 行那个），新增：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runner.py::test_hard_interrupt_aborts_inflight -q`
Expected: FAIL（现为软取消：`work` 不会中止在途 chat，`fake` 卡在 `release.wait()`，`wait_for(task)` 超时）

- [ ] **Step 3: 加 `_cancellable` 助手**

`backend/app/engine/runner.py`，在 `_now` 之后加：

```python
async def _cancellable(coro, cancel_event: asyncio.Event):
    """运行 coro，期间若 cancel_event 置位则立刻中止 coro（硬中断）；被中止时抛 CancelledError。
    若 coro 自身完成（含抛业务异常）则正常返回/抛出，不受影响。"""
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise asyncio.CancelledError
```

- [ ] **Step 4: 改 `_run_llm_node.work` 用 `_cancellable`**

把 `work(idx)` 的 try 块改为：

```python
    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await _cancellable(
                    nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem), cancel_event)
            except asyncio.CancelledError:
                return  # 硬中断：在途请求已 abort，该行不落库（保持 pending）
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                failed_count += 1
            else:
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "", usage=usage)
                done_count += 1
            await _set_node_state(session_factory, run_id, node.id, status="running",
                                  total=total, done=done_count, failed=failed_count)
```

- [ ] **Step 5: 跑硬中断测试 + 既有取消测试**

Run: `cd backend && uv run pytest tests/test_runner.py -q -k "cancel or hard or happy or row_failure or resume or fanout"`
Expected: PASS（`test_hard_interrupt_aborts_inflight`、`test_cancel_before_llm` 等全过）

- [ ] **Step 6: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_runner.py
git commit -m "feat: 运行硬中断——_cancellable 立即 abort 进行中的 LLM 请求" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `nodes.run_qc_judge_row` + 删规则 `qc_split`（§5a）

**Files:**
- Modify: `backend/app/engine/nodes.py`（删 `qc_split`，加 `run_qc_judge_row`；`_predicate` 保留给 filter）
- Test: `backend/tests/test_qc.py`（整文件重写）

- [ ] **Step 1: 重写 `test_qc.py`（失败测试）**

把 `backend/tests/test_qc.py` 整个替换为：

```python
import asyncio
import json

import pytest

from app.engine import nodes
from app.services import llm


async def test_qc_judge_parses_verdict(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        assert params and params.get("json_mode") is True  # 判定强制 json 模式
        assert "译文:hello" in user  # 用 base 渲染（剥离 _qc_reason）
        return json.dumps({"pass": False, "reason": "不是中文"}), {"prompt_tokens": 2, "completion_tokens": 3}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage = await nodes.run_qc_judge_row(
        {"user_prompt": "译文:{{a}}"}, {"a": "hello", "_qc_reason": "旧"}, None, asyncio.Semaphore(1))
    assert ok is False and reason == "不是中文"
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


async def test_qc_judge_pass(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"pass": True}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _ = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, None, asyncio.Semaphore(1))
    assert ok is True and reason == "未通过质检"  # reason 缺省给通用文案


async def test_qc_judge_missing_pass_raises(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"reason": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    with pytest.raises(ValueError):
        await nodes.run_qc_judge_row({"user_prompt": "p"}, {"a": "x"}, None, asyncio.Semaphore(1))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_qc.py -q`
Expected: FAIL（`nodes.run_qc_judge_row` 不存在 → AttributeError）

- [ ] **Step 3: 删 `qc_split`，加 `run_qc_judge_row`**

`backend/app/engine/nodes.py`：**删除** `qc_split` 函数（114-128 行整块，含其 docstring）。在 `run_llm_synth_row` 之后**追加**：

```python
async def run_qc_judge_row(config: dict, row: dict, mc: ModelConfig,
                           user_sem: asyncio.Semaphore) -> tuple[bool, str, dict]:
    """质检判定一行：渲染判定提示词 → LLM（强制 json 模式）→ 解析 {"pass","reason"}。
    返回 (是否通过, 原因, usage)。复用 render_template + llm.chat（与 LLM 合成节点同原语）。"""
    base = {k: v for k, v in row.items() if k != "_qc_reason"}
    system = render_template(config.get("system_prompt", ""), base)
    user = render_template(config.get("user_prompt", ""), base)
    params = {**config.get("params", {}), "json_mode": True}
    async with user_sem:
        text, usage = await llm.chat(mc, system, user, params=params,
                                     retries=config.get("retries", 3))
    verdict = _json.loads(text)
    if "pass" not in verdict:
        raise ValueError("质检判定未返回 pass 字段")
    return bool(verdict["pass"]), str(verdict.get("reason") or "未通过质检"), usage
```

（`_json` 已在文件顶部 `import json as _json`；`ModelConfig` 已 import；`llm` 已 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_qc.py -q`
Expected: PASS（3 用例全过）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/nodes.py backend/tests/test_qc.py
git commit -m "feat: 质检改 LLM 判定——run_qc_judge_row（复用 llm.chat），删规则 qc_split" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `runner._run_qc_node` LLM 判定 + 回扫（§5b）

**Files:**
- Modify: `backend/app/engine/runner.py`（重写 `_run_qc_node`）
- Test: `backend/tests/test_runner.py`（改 `RESCAN_GRAPH`、`make_run`、两个回扫用例）

- [ ] **Step 1: 改 `make_run` 给 qc 节点也填模型，改 `RESCAN_GRAPH` 为 LLM 判定版**

`backend/tests/test_runner.py`：

把 `make_run` 中的节点循环改为（qc 也填 model_config_id）：

```python
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] in ("llm_synth", "qc"):
                n["config"]["model_config_id"] = mc.id
```

把 `RESCAN_GRAPH` 的 qc 节点 config 改为：

```python
        {"id": "qc", "type": "qc",
         "config": {"model_config_id": 0, "user_prompt": "判定:{{a}}", "max_rounds": 2}},
```

- [ ] **Step 2: 重写两个回扫用例（失败测试）**

把 `test_rescan_regenerates_failed_rows` 与 `test_rescan_drops_persistent_failures` 替换为：

```python
def _rescan_fn(persistent):
    def fn(user):
        if user.startswith("判定:"):  # 质检判定调用：含 bad 即不通过
            bad = "bad" in user
            return json.dumps({"pass": not bad, "reason": "含bad" if bad else ""}), \
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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runner.py -q -k rescan`
Expected: FAIL（现 `_run_qc_node` 调用规则版 `qc_split`，已被删 → AttributeError / 判定走错）

- [ ] **Step 4: 重写 `_run_qc_node`**

`backend/app/engine/runner.py`，把整个 `_run_qc_node` 替换为：

```python
async def _run_qc_node(session_factory, run_id, user_id, graph: Graph, node: Node, inputs,
                       user_sem, cancel_event):
    """质检节点：逐行用 LLM 判定通过/不通过；不通过的行带原因经 rescan 回扫边的 LLM 重生成，
    再复判，最多 max_rounds 轮，仍不过的行丢弃。仅持久化最终通过行（含各轮 token 汇总）。"""
    cfg = node.config
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
        jmc = await s.get(ModelConfig, cfg.get("model_config_id"))
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, status="done",
                              total=len(inputs), done=len(inputs), failed=0)
        return
    if jmc is None or jmc.user_id != user_id:
        raise ValueError(f"质检节点 {node.id}: 判定模型配置不存在")
    await _set_node_state(session_factory, run_id, node.id, status="running",
                          total=len(inputs), done=0, failed=0)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def fold(u):
        usage["prompt_tokens"] += u["prompt_tokens"]
        usage["completion_tokens"] += u["completion_tokens"]

    sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def judge_all(rows):
        async def judge(row):
            async with sem:
                return await _cancellable(nodes.run_qc_judge_row(cfg, row, jmc, user_sem), cancel_event)
        passed_, failed_ = [], []
        for row, (ok, reason, u) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append(row)
            else:
                failed_.append({**row, "_qc_reason": reason})
        return passed_, failed_

    try:
        passed, failed = await judge_all(inputs)
        rounds = 0
        target_id = next((e["target"] for e in graph.edges
                          if e["kind"] == "rescan" and e["source"] == node.id), None)
        if target_id is not None and failed:
            tgt = next(n for n in graph.nodes if n.id == target_id)
            async with session_factory() as s:
                tmc = await s.get(ModelConfig, tgt.config.get("model_config_id"))
            if tmc is None or tmc.user_id != user_id:
                raise ValueError(f"回扫目标 {target_id}: 模型配置不存在")
            rsem = asyncio.Semaphore(tgt.config.get("concurrency", 4))

            async def regen(row):
                async with rsem:
                    return await _cancellable(
                        nodes.run_llm_synth_row(tgt.config, row, tmc, user_sem), cancel_event)

            while failed and rounds < cfg.get("max_rounds", 3):
                rounds += 1
                regenerated: list[dict] = []
                for out_rows, u in await asyncio.gather(*[regen(r) for r in failed]):
                    fold(u)
                    regenerated.extend(out_rows)
                fresh_pass, failed = await judge_all(regenerated)
                passed.extend(fresh_pass)
                await _set_node_state(session_factory, run_id, node.id, status="running",
                                      total=len(inputs), done=len(passed), failed=len(failed))
    except asyncio.CancelledError:
        return  # 硬中断：不落库
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, status="failed",
                              total=len(inputs), done=0, failed=len(inputs))
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", passed, "",
                      usage=usage, qc_round=rounds)
    await _set_node_state(session_factory, run_id, node.id, status="done",
                          total=len(inputs), done=len(passed), failed=len(inputs) - len(passed))
```

- [ ] **Step 5: 跑回扫测试 + 整个 runner 套件**

Run: `cd backend && uv run pytest tests/test_runner.py -q`
Expected: PASS（回扫两测 + happy/cancel/fanout 等全过）

- [ ] **Step 6: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_runner.py
git commit -m "feat: 质检节点 LLM 逐行判定+回扫重生成（硬中断感知），token 折叠汇总" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: create_run 校验 qc 判定模型归属（§5c）

**Files:**
- Modify: `backend/app/routers/runs.py`（48-57 行的归属校验块）
- Test: `backend/tests/test_runs_api.py`（新增一个用例）

- [ ] **Step 1: 写失败测试**

先看 `backend/tests/test_runs_api.py` 现有用例的建图/登录辅助（沿用其同款 fixture 与 helper）。新增：构造一个含 qc 节点但 qc 未配模型的工作流，POST `/api/runs` 期望 422 且 detail 含节点 id。示例（按该文件已有 helper 命名微调）：

```python
async def test_create_run_rejects_qc_without_model(auth_client):
    # 上传一个数据集拿到 id（沿用本文件已有的上传/建集 helper；若无则最简内联）
    graph = {"nodes": [
        {"id": "input_1", "type": "input", "config": {"dataset_ids": []}},
        {"id": "qc_1", "type": "qc", "config": {"user_prompt": "判:{{a}}"}},  # 缺 model_config_id
    ], "edges": [{"source": "input_1", "target": "qc_1", "kind": "normal"}]}
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    r = await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})
    assert r.status_code == 422 and "qc_1" in r.json()["detail"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runs_api.py::test_create_run_rejects_qc_without_model -q`
Expected: FAIL（当前 create_run 不校验 qc 模型 → 可能放行或在别处报不同错）

- [ ] **Step 3: 把 llm_synth 的模型归属校验扩到 qc**

`backend/app/routers/runs.py` create_run 里的：

```python
        if n.type == "llm_synth":
            mc = await session.get(ModelConfig, n.config.get("model_config_id"))
            if mc is None or mc.user_id != user.id:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择有效的模型配置")
```

改为：

```python
        if n.type in ("llm_synth", "qc"):
            mc = await session.get(ModelConfig, n.config.get("model_config_id"))
            if mc is None or mc.user_id != user.id:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择有效的模型配置")
```

- [ ] **Step 4: 跑测试 + runs_api 全套**

Run: `cd backend && uv run pytest tests/test_runs_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/runs.py backend/tests/test_runs_api.py
git commit -m "feat: create_run 校验质检节点判定模型归属当前用户" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 节点助手后端 `/api/agent/node-assist`（§8a）

**Files:**
- Modify: `backend/app/agent/codegen.py`（加 `generate_node_config`）
- Modify: `backend/app/routers/agent.py`（加 `NodeAssistIn` + 端点）
- Test: `backend/tests/test_agent_codegen.py`（加单元测试）、`backend/tests/test_agent_api.py`（加端点守卫测试）

- [ ] **Step 1: 写失败测试（单元）**

在 `backend/tests/test_agent_codegen.py` 末尾追加：

```python
async def test_generate_node_config_llm_synth():
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    out = json.dumps({"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"},
                     ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```json\n{out}\n```")]))
    cfg = await codegen.generate_node_config(model, "llm_synth", "把 q 翻译成英文", [{"q": "你好"}])
    assert cfg == {"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}


async def test_generate_node_config_rejects_unknown_type():
    import pytest
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("{}")]))
    with pytest.raises(KeyError):
        await codegen.generate_node_config(model, "input", "x", [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_agent_codegen.py::test_generate_node_config_llm_synth -q`
Expected: FAIL（`codegen.generate_node_config` 不存在）

- [ ] **Step 3: 加 `generate_node_config`**

`backend/app/agent/codegen.py`，在 `generate_with_repair` 之后追加：

```python
NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": """你为「LLM 合成」节点写配置：根据用户指令和样本可用列，写一段生成提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 形如 {"system_prompt": "...", "user_prompt": "...", "output_column": "..."}。
- user_prompt 用 {{列名}} 引用样本里的可用列。""",
    "qc": """你为「质检」节点写判定配置：根据用户指令和样本可用列，写一段判定提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 形如 {"system_prompt": "...", "user_prompt": "..."}。
- 提示词要引导模型只输出 {"pass": true|false, "reason": "<不通过原因>"}。
- user_prompt 用 {{列名}} 引用样本里的可用列。""",
}


async def generate_node_config(model, node_type: str, instruction: str, sample_rows: list[dict]) -> dict:
    """临时单 Agent 为指定节点产出配置 JSON（不跑代码，仅生成提示词）。未知 node_type 抛 KeyError。"""
    agent = create_agent(model, [], NODE_ASSIST_INSTRUCTIONS[node_type])
    result = await agent.run(_user_prompt(instruction, sample_rows))
    return json.loads(strip_code_fences(str(result.output or "")))
```

文件顶部 import 加上 `from app.agent.factory import create_agent`（codegen 现已 import 它用于 `generate_with_repair`；若已存在则跳过）。

- [ ] **Step 4: 单元测试通过；写端点守卫失败测试**

Run: `cd backend && uv run pytest tests/test_agent_codegen.py -q` → PASS

在 `backend/tests/test_agent_api.py` 追加（沿用其 `auth_client` 与建模型/工作流 helper；下例用最简内联，按文件实际 helper 命名调整）：

```python
async def test_node_assist_guards(auth_client, monkeypatch):
    from app.agent import codegen
    async def fake_cfg(model, node_type, instruction, sample_rows):
        return {"system_prompt": "s", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}
    monkeypatch.setattr(codegen, "generate_node_config", fake_cfg)
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "x", "base_url": "http://x", "api_key": "k"})).json()
    # 成功路径
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "llm_synth_1", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": mc["id"]})
    assert r.status_code == 200 and r.json()["config"]["output_column"] == "q_en"
    # 不支持的节点类型
    r2 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "input_1", "node_type": "input",
        "instruction": "x", "model_config_id": mc["id"]})
    assert r2.status_code == 422
    # 他人工作流 → 404
    r3 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": 99999, "node_id": "n", "node_type": "qc",
        "instruction": "x", "model_config_id": mc["id"]})
    assert r3.status_code == 404
```

- [ ] **Step 5: 跑端点测试确认失败**

Run: `cd backend && uv run pytest tests/test_agent_api.py::test_node_assist_guards -q`
Expected: FAIL（端点不存在 → 404 for all / 405）

- [ ] **Step 6: 加端点**

`backend/app/routers/agent.py`：import 行把 `from app.agent.codegen import gather_sample_rows, generate_with_repair` 改为 `from app.agent.codegen import gather_sample_rows, generate_node_config, generate_with_repair`。在 `codegen` 端点之后追加：

```python
class NodeAssistIn(BaseModel):
    workflow_id: int
    node_id: str
    node_type: str
    instruction: str
    model_config_id: int


@router.post("/node-assist")
async def node_assist(body: NodeAssistIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    if body.node_type not in ("llm_synth", "qc"):
        raise HTTPException(status_code=422, detail="该节点类型不支持助手")
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    mc = await session.get(ModelConfig, body.model_config_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=422, detail="模型配置无效")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="指令不能为空")
    sample_rows, source = await gather_sample_rows(session, body.workflow_id, body.node_id, user.id)
    config = await generate_node_config(mc, body.node_type, body.instruction, sample_rows)
    return {"config": config, "sample_source": source}
```

- [ ] **Step 7: 跑端点测试 + agent 全套**

Run: `cd backend && uv run pytest tests/test_agent_api.py tests/test_agent_codegen.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add backend/app/agent/codegen.py backend/app/routers/agent.py backend/tests/test_agent_codegen.py backend/tests/test_agent_api.py
git commit -m "feat: 节点助手后端——/api/agent/node-assist 为 LLM/质检节点生成提示词配置" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: CLI 质检键改 LLM 版（§5d）

**Files:**
- Modify: `backend/app/cli.py`（`cmd_node_set` 删规则 qc 分支）
- Test: `backend/tests/test_cli.py`（改 `test_qc_node_set_and_rescan_link`）

- [ ] **Step 1: 改测试（失败测试）**

`backend/tests/test_cli.py` 的 `test_qc_node_set_and_rescan_link`：把 `gf("node", "set", "qc_1", ...)` 与断言改为 LLM 版：

```python
def test_qc_node_set_and_rescan_link(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "add", "qc")
    gf("node", "set", "qc_1", "system=你是质检员", "prompt=判定:{{a}}", "max_rounds=2")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    node = json.loads(capsys.readouterr().out)
    assert node["type"] == "qc"
    assert node["config"]["system_prompt"] == "你是质检员"
    assert node["config"]["user_prompt"] == "判定:{{a}}"
    assert node["config"]["max_rounds"] == 2
    gf("link", "llm_synth_1", "qc_1")
    capsys.readouterr()
    gf("link", "qc_1", "llm_synth_1", "--kind", "rescan")
    assert "回扫" in capsys.readouterr().out
    gf("show")
    assert "qc_1 ⟲回扫 llm_synth_1" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_cli.py::test_qc_node_set_and_rescan_link -q`
Expected: FAIL（断言 system_prompt 等不成立，旧分支写的是 condition）

- [ ] **Step 3: 删 `cmd_node_set` 的规则 qc 分支**

`backend/app/cli.py` `cmd_node_set` 里**删除**这两段：

```python
        elif k in ("qc_col", "qc_mode", "qc_value"):
            field = {"qc_col": "column", "qc_mode": "mode", "qc_value": "value"}[k]
            val = int(v) if k == "qc_value" and v.lstrip("-").isdigit() else v
            cfg.setdefault("condition", {})[field] = val
```

以及 `reason`/`reason_field` 分支：

```python
        elif k in ("reason", "reason_field"):
            cfg[k] = v
```

保留 `max_rounds` 分支。（`system`/`prompt`/`model`/`conc` 已由 `LLM_CONFIG_KEYS` 处理，qc 直接复用。）

- [ ] **Step 4: 跑测试 + cli 全套**

Run: `cd backend && uv run pytest tests/test_cli.py -q`
Expected: PASS（`test_rescan_from_non_qc_node_dies` 不受影响）

- [ ] **Step 5: 提交**

```bash
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf node set 质检键改 LLM 版（system/prompt/model/max_rounds），删规则键" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 更新 gf-cli 技能文档（§5e）

**Files:**
- Modify: `.claude/skills/gf-cli/SKILL.md`、`.claude/skills/gf-cli/reference.md`

- [ ] **Step 1: 改两处文档**

把质检节点的描述从「规则判定（qc_col/qc_mode/qc_value）」改为「LLM 语义判定」。具体：
- 搜索 `qc_col`、`qc_mode`、`qc_value`、`reason_field`，替换为新键说明：`gf node set qc_1 model=<模型> system=<判定规则> prompt=<判定:{{列}}> max_rounds=2`。
- 在质检/回扫小节注明：质检节点用 LLM 逐行判定，判定提示词需引导模型输出 `{"pass": true|false, "reason": "..."}`；不通过的行带原因经 `--kind rescan` 回扫边的上游 LLM 重生成。
- 保留 `gf link --kind rescan`、`⟲回扫` 渲染、"质检回扫支持，别回复'做不到'"等既有表述。

Run（确认已无旧键残留）: `cd "E:/代码/GraphFlow" && grep -rn "qc_col\|qc_mode\|qc_value\|reason_field" .claude/skills/gf-cli/ || echo "clean"`
Expected: `clean`

- [ ] **Step 2: 提交**

```bash
git add .claude/skills/gf-cli/SKILL.md .claude/skills/gf-cli/reference.md
git commit -m "docs: gf-cli 技能文档质检改 LLM 判定版" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 数据集页列表置顶 + 上传小按钮（§3）

**Files:**
- Modify: `frontend/src/pages/DatasetsPage.tsx`

- [ ] **Step 1: 换掉 Dragger**

`frontend/src/pages/DatasetsPage.tsx`：
- import 行改为 `import { Button, Drawer, Popconfirm, Space, Table, Upload, message } from 'antd'`，并把 `import { InboxOutlined } from '@ant-design/icons'` 改为 `import { UploadOutlined } from '@ant-design/icons'`。
- 把 `return ( <> ... </> )` 里的 `<Upload.Dragger ...>...</Upload.Dragger>` 整块替换为：

```tsx
      <Space style={{ marginBottom: 16 }}>
        <Upload
          multiple
          accept=".jsonl,.json,.csv,.xlsx,.xls"
          beforeUpload={(_, fileList) => {
            void doUpload(fileList as unknown as File[])
            return false
          }}
          showUploadList={false}
        >
          <Button type="primary" icon={<UploadOutlined />}>上传数据集（JSONL / JSON / CSV / Excel，可多选）</Button>
        </Upload>
      </Space>
```

（`<Table>` 与 `<Drawer>` 保持不变，现在紧随工具栏置顶。）

- [ ] **Step 2: 构建 + 既有前端测试**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS（vitest 10 过；build 无 TS 报错）

- [ ] **Step 3: 提交**

```bash
git add frontend/src/pages/DatasetsPage.tsx
git commit -m "feat: 数据集页列表置顶，上传改紧凑按钮" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 画布节点变动自动保存（§4）

**Files:**
- Modify: `frontend/src/pages/CanvasPage.tsx`

- [ ] **Step 1: 加防抖自动保存 effect**

`frontend/src/pages/CanvasPage.tsx`，在 `onConnect` 的 `useCallback` 之后、`addNode` 之前插入：

```tsx
  // 节点/连线变动后防抖自动保存：指纹与 baseline 不同才真正 PUT（初次 load 设了 baseline，故不触发）
  useEffect(() => {
    const t = setTimeout(() => {
      const graph = fromFlow(nodes, edges)
      if (graphFingerprint(graph) === baseline.current) return
      baseline.current = graphFingerprint(graph)
      void api.put(`/api/workflows/${id}`, { graph })
    }, 800)
    return () => clearTimeout(t)
  }, [nodes, edges, id])
```

（`fromFlow`、`graphFingerprint`、`baseline`、`api` 均已在文件内可用。手动「保存」按钮保留。自存触发的 SSE 自回声会进 `useEvents`，因指纹等于 baseline 仅做幂等重载，无害。）

- [ ] **Step 2: 构建 + 既有前端测试**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add frontend/src/pages/CanvasPage.tsx
git commit -m "feat: 画布节点/连线变动 800ms 防抖自动保存" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: 输入节点内联数据集预览（§6）

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`InputNodeForm` + 新增 `DatasetHeadPreview`）

- [ ] **Step 1: 加预览组件并接进 InputNodeForm**

`frontend/src/canvas/forms/NodeConfigForm.tsx`：
- import 行加入 `Table`：`import { Button, Input, InputNumber, Radio, Select, Space, Switch, Table } from 'antd'`。
- `import type { CodegenOut, Dataset, ModelConfig } from '../../api/types'` 改为 `import type { CodegenOut, Dataset, ModelConfig, RowsPage } from '../../api/types'`。
- 把 `InputNodeForm` 替换为：

```tsx
function DatasetHeadPreview({ ds }: { ds: Dataset }) {
  const [rows, setRows] = useState<Record<string, any>[]>([])
  useEffect(() => {
    void api.get<RowsPage>(`/api/datasets/${ds.id}/rows?page=1&page_size=5`).then((r) => setRows(r.rows))
  }, [ds.id])
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ color: '#666', marginBottom: 4 }}>{ds.name}（前 {rows.length} 行 / 共 {ds.row_count}）</div>
      <Table
        size="small" rowKey={(_, i) => String(i)} pagination={false} dataSource={rows}
        scroll={{ x: 'max-content' }}
        columns={ds.columns.map((c) => ({
          title: c, dataIndex: c, ellipsis: true,
          render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')),
        }))}
      />
    </div>
  )
}

function InputNodeForm({ config, onChange }: FormProps) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  useEffect(() => {
    void api.get<Dataset[]>('/api/datasets').then(setDatasets)
  }, [])
  const selected = (config.dataset_ids ?? [])
    .map((id: number) => datasets.find((d) => d.id === id))
    .filter(Boolean) as Dataset[]
  return (
    <>
      <Field label="数据集（可多选，按行拼接）">
        <Select
          mode="multiple" style={{ width: '100%' }} value={config.dataset_ids ?? []}
          onChange={(v) => onChange({ ...config, dataset_ids: v })}
          options={datasets.map((d) => ({ value: d.id, label: `${d.name}（${d.row_count} 行）` }))}
        />
      </Field>
      {selected.map((d) => <DatasetHeadPreview key={d.id} ds={d} />)}
    </>
  )
}
```

- [ ] **Step 2: 构建 + 既有前端测试**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat: 输入节点选中数据集后内联预览列+前5行（截断省略，只取头部）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: 节点助手前端 + QcForm LLM 版（§5f / §8b）

**Files:**
- Modify: `frontend/src/api/types.ts`（加 `NodeAssistOut`）
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（加 `NodeAssist`；改 `LlmSynthForm`、`QcForm`、dispatch 透传 props）

- [ ] **Step 1: types.ts 加返回类型**

`frontend/src/api/types.ts` 末尾追加：

```ts
export interface NodeAssistOut {
  config: Record<string, any>
  sample_source: 'last_run' | 'dataset' | 'none'
}
```

- [ ] **Step 2: 加 `NodeAssist` 组件**

`NodeConfigForm.tsx`：`import type` 行加上 `NodeAssistOut`。在 `LlmSynthForm` 之前插入：

```tsx
function NodeAssist({ nodeType, workflowId, nodeId, onApply }: {
  nodeType: string; workflowId?: number; nodeId?: string
  onApply: (config: Record<string, any>) => void
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [info, setInfo] = useState('')
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const run = async () => {
    if (!modelSel || !workflowId || !nodeId) return
    setBusy(true)
    setInfo('')
    try {
      const r = await api.post<NodeAssistOut>('/api/agent/node-assist', {
        workflow_id: workflowId, node_id: nodeId, node_type: nodeType,
        instruction, model_config_id: modelSel,
      })
      onApply(r.config)
      if (r.sample_source === 'none') setInfo('没有可用样本（先保存画布、运行一次更准）')
    } catch (e) {
      setInfo((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div style={{ border: '1px dashed #d9d9d9', borderRadius: 6, padding: 8, marginBottom: 12 }}>
      <div style={{ color: '#722ed1', marginBottom: 4 }}>RedLotus 助手：描述需求，自动写提示词</div>
      <Input.TextArea rows={2} value={instruction} placeholder="如：把 q 列翻译成英文存到 q_en"
                      onChange={(e) => setInstruction(e.target.value)} />
      <Space style={{ marginTop: 8 }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={setModelSel} options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={busy} disabled={!instruction || !modelSel}
                onClick={() => void run()}>让 RedLotus 配置</Button>
      </Space>
      {info && <div style={{ color: '#d46b08', fontSize: 12, marginTop: 4 }}>{info}</div>}
    </div>
  )
}
```

- [ ] **Step 3: `LlmSynthForm` 接 props + 顶部加助手**

把 `LlmSynthForm` 签名改为 `function LlmSynthForm({ config, onChange, workflowId, nodeId }: FormProps & { workflowId?: number; nodeId?: string })`，并在其 `return (<> ... )` 的最前面（`<Field label="模型">` 之前）插入：

```tsx
      <NodeAssist nodeType="llm_synth" workflowId={workflowId} nodeId={nodeId}
                  onApply={(c) => onChange({ ...config, ...c })} />
```

- [ ] **Step 4: 重写 `QcForm` 为 LLM 版**

把整个 `QcForm` 替换为：

```tsx
function QcForm({ config, onChange, workflowId, nodeId }: FormProps & {
  workflowId?: number; nodeId?: string
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  return (
    <>
      <NodeAssist nodeType="qc" workflowId={workflowId} nodeId={nodeId}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Field label="判定模型">
        <Select style={{ width: '100%' }} value={config.model_config_id}
                onChange={(v) => patch({ model_config_id: v })}
                options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))} />
      </Field>
      <Field label='System Prompt（判定规则；要求模型只输出 {"pass":true|false,"reason":"..."}）'>
        <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                        onChange={(e) => patch({ system_prompt: e.target.value })} />
      </Field>
      <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
        <Input.TextArea rows={5} value={config.user_prompt ?? ''}
                        onChange={(e) => patch({ user_prompt: e.target.value })} />
      </Field>
      <Field label="最多回扫轮数">
        <InputNumber min={0} value={config.max_rounds ?? 3}
                     onChange={(v) => patch({ max_rounds: v ?? 3 })} />
      </Field>
      <div style={{ color: '#999', fontSize: 12 }}>
        把质检节点底部的橙色圆点拖回上游 LLM 节点形成回扫边；不通过的行带原因重生成，满 N 轮仍不过则丢弃。
      </div>
    </>
  )
}
```

（`LEN_MODES` 常量若仅 QcForm 用到则会变成未使用——它也用于 `OpFields` 的 filter，保留不动。）

- [ ] **Step 5: dispatch 透传 props 给 llm_synth 和 qc**

把 `NodeConfigForm` 的 switch 改为：

```tsx
    case 'input':
      return <InputNodeForm config={config} onChange={onChange} />
    case 'llm_synth':
      return <LlmSynthForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} />
    case 'auto_process':
      return <AutoProcessForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} />
    case 'qc':
      return <QcForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} />
    case 'output':
      return <OutputNodeForm config={config} onChange={onChange} />
```

- [ ] **Step 6: 构建 + 既有前端测试**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS（若某 vitest 用例断言了旧 QcForm 规则字段，按新版字段更新；预计 10 个用例不涉及表单内部）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat: 节点 RedLotus 助手（LLM/质检），质检表单改判定模型+提示词" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 全量回归 + 收尾（§9）

**Files:** 无（仅验证）

- [ ] **Step 1: 后端全量**

Run: `cd backend && uv run pytest -q`
Expected: PASS（全绿；若有遗漏的旧 qc 规则引用导致失败，定位修复后再跑）

- [ ] **Step 2: 前端全量**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS

- [ ] **Step 3: 确认无脏键残留**

Run: `cd "E:/代码/GraphFlow" && grep -rn "qc_split\|qc_col\|condition\b" backend/app/ || echo "clean"`
Expected: `clean`（`condition` 仅可能出现在 filter 算子语境；若 grep 命中 filter 的合法用法可忽略，关注的是 qc 残留）

- [ ] **Step 4: 若工作区干净则无需额外提交**

Run: `cd "E:/代码/GraphFlow" && git status --short`
Expected: 仅剩既有未跟踪噪声（`.idea/`、`.codegraph/`、`项目设计.txt` 等），**不要**提交它们。

---

## 自查记录（写作者已核对）

- **Spec 覆盖**：§2→T1、§3→T9、§4→T10、§5(QC)→T3/T4/T5/T7/T8/T12、§6→T11、§7→T2、§8→T6/T12。全覆盖。
- **类型/签名一致**：`session_dir(username, session_id)`（T1）三处调用一致；`run_qc_judge_row(config,row,mc,user_sem)->(bool,str,dict)`（T3）被 T4 `judge_all` 调用签名一致；`_cancellable(coro,cancel_event)`（T2）被 T4 judge/regen 复用一致；`generate_node_config(model,node_type,instruction,sample_rows)->dict`（T6）端点与测试一致；前端 `NodeAssistOut`（T12）与端点返回 `{config, sample_source}`（T6）一致。
- **无占位符**：每步均含实际代码/命令/期望。
- **顺序依赖**：T2 的 `_cancellable` 必须先于 T4（T4 复用它）；T3 的 `run_qc_judge_row` 先于 T4。已按此排序。
