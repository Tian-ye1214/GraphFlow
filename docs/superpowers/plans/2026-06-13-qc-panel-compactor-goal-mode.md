# 多模型质检 + 上下文 Compactor + 目标优化模式 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让质检节点支持多模型 K-of-N 判定，新增可复用的上下文 Compactor，并把 RedLotus 目标模式升级为可度量、事件驱动、凝练经验式的自我改进循环。

**Architecture:** 后端 FastAPI + SQLAlchemy async（SQLite，`create_all` 只建新表不加列），pydantic-ai 1.107 多角色 Agent。F1 改 QC 节点判定 + 两张新表（QcMetric/QcFailure）；F2 新增 `model_meta.py`（OpenRouter 窗口）+ `compactor.py`（在 `agent.run` 前压缩历史，不用已废弃的 history_processor）；F3 用 RunManager 完成事件做事件驱动等待，turns.py 跑 while-True 目标循环。前端 React 19 + antd 6。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2 async / pydantic-ai-slim 1.107 / pytest + httpx ASGITransport；React 19 / antd 6 / vitest。

**贯穿约束（KISS 硬规则）：** 最简实现，不预防未发生的 bug；api_key 全程 Fernet 加密、绝不进响应/日志/提示词；所有模型引用校验 `user_id`（租户隔离）。提交格式两个 `-m`，第二个为 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。**禁止 `git add`** `项目设计.txt`、`.idea/`、`.codegraph/`。

**测试运行约定：** 后端 `cd backend && python -m pytest <path> -v`（PowerShell 用 `;` 串联，不用 `&&`）。前端 `cd frontend && npm run build` 与 `npx vitest run <path>`。

**文件结构总览：**
- 改：`backend/app/models.py`（+QcMetric +QcFailure）、`backend/app/engine/nodes.py`（多模型判定）、`backend/app/engine/runner.py`（QC 节点改造 + 指标/失败样本落库 + _finish publish）、`backend/app/engine/manager.py`（完成事件 + wait）、`backend/app/routers/runs.py`（校验/级联/新 API）、`backend/app/routers/admin.py`（级联补表）、`backend/app/routers/agent.py`（compactor 角色 + goal 入口）、`backend/app/agent/system.py` `orchestrator.py` `turns.py`（compaction seam + goal 循环）、`backend/app/config.py`（goal_no_improve_k）、`backend/app/cli.py`（node set 多模型 + wf restore）。
- 增：`backend/app/agent/model_meta.py`、`backend/app/agent/compactor.py`、`backend/app/services/run_service.py`、`backend/app/agent/goal_loop.py`。
- 前端改：`frontend/src/canvas/forms/NodeConfigForm.tsx`（QcForm 多选）、`frontend/src/pages/RunDetailPage.tsx`（质检失败样本区 + 恢复版本）、`frontend/src/agent/AgentDrawer.tsx`（目标启动 + 指标）、`frontend/src/api/types.ts`。

---

# F1 · 多模型质检面板（K-of-N）

## Task 1: QcMetric / QcFailure 数据表

**Files:**
- Modify: `backend/app/models.py`（在 `RunLog` 类后追加）
- Test: `backend/tests/test_models_qc.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_models_qc.py`：
```python
from app.models import QcFailure, QcMetric


async def test_qc_metric_and_failure_insert(session_factory):
    async with session_factory() as s:
        s.add(QcMetric(run_id=1, node_id="qc1", total=10, first_round_pass=7))
        s.add(QcFailure(run_id=1, node_id="qc1", sample_json='{"q":"x"}',
                        reasons_json='[{"model_config_id":2,"pass":false,"reason":"太短"}]'))
        await s.commit()
    async with session_factory() as s:
        from sqlalchemy import select
        m = (await s.execute(select(QcMetric))).scalar_one()
        f = (await s.execute(select(QcFailure))).scalar_one()
    assert m.total == 10 and m.first_round_pass == 7
    assert f.node_id == "qc1" and "太短" in f.reasons_json
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_models_qc.py -v`
Expected: FAIL（`ImportError: cannot import name 'QcMetric'`）

- [ ] **Step 3: 加模型**

在 `backend/app/models.py` 的 `RunLog` 类（约 131 行）之后追加：
```python
class QcMetric(Base):
    __tablename__ = "qc_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    total: Mapped[int] = mapped_column(default=0)
    first_round_pass: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QcFailure(Base):
    __tablename__ = "qc_failures"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    sample_json: Mapped[str] = mapped_column(Text, default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_models_qc.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models.py backend/tests/test_models_qc.py
git commit -m "feat(qc): QcMetric/QcFailure 数据表" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 多模型 K-of-N 判定（nodes.run_qc_judge_row）

**Files:**
- Modify: `backend/app/engine/nodes.py:121-157`（`run_llm_synth_row` 改剥离前缀）与 `:160-174`（`run_qc_judge_row` 重写）
- Test: `backend/tests/test_qc_multi.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_qc_multi.py`（用桩替换 `llm.chat`，按模型 id 给不同判词）：
```python
import asyncio
import json

import app.engine.nodes as nodes


def _fake_chat_factory(verdict_by_model):
    async def fake_chat(mc, system, user, params=None, retries=3):
        v = verdict_by_model[mc.id]
        return json.dumps(v), {"prompt_tokens": 1, "completion_tokens": 1}
    return fake_chat


class _MC:
    def __init__(self, id): self.id = id


async def test_k_of_n_pass(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"pass": True, "reason": "好"}, 2: {"pass": False, "reason": "太短"},
        3: {"pass": True, "reason": "好"}}))
    sem = asyncio.Semaphore(4)
    cfg = {"system_prompt": "", "user_prompt": "{{q}}"}
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        cfg, {"q": "hello"}, [_MC(1), _MC(2), _MC(3)], 2, sem)
    assert ok is True                       # 2/3 通过 ≥ K=2
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3}
    assert {p["model_config_id"] for p in per_model} == {1, 2, 3}


async def test_k_of_n_fail_aggregates_reasons(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"pass": False, "reason": "太短"}, 2: {"pass": False, "reason": "跑题"}}))
    sem = asyncio.Semaphore(4)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"system_prompt": "", "user_prompt": "{{q}}"}, {"q": "x"}, [_MC(1), _MC(2)], 2, sem)
    assert ok is False                      # 0/2 ≥ 2 → 不通过
    assert "太短" in reason and "跑题" in reason
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_qc_multi.py -v`
Expected: FAIL（`run_qc_judge_row` 旧签名只收单个 mc）

- [ ] **Step 3: 重写 run_qc_judge_row 并加内部键剥离**

在 `backend/app/engine/nodes.py`，把 `run_llm_synth_row` 里的 `base = {k: v for k, v in row.items() if k != "_qc_reason"}`（约 124 行）改为剥离所有 `_qc` 前缀键：
```python
    base = {k: v for k, v in row.items() if not k.startswith("_qc")}
```
然后把 `run_qc_judge_row`（160-174 行）整体替换为：
```python
async def run_qc_judge_row(config: dict, row: dict, mcs: list[ModelConfig], pass_k: int,
                           user_sem: asyncio.Semaphore) -> tuple[bool, str, dict, list]:
    """多模型 K-of-N 质检判定：N 个模型共用提示词并发判定，≥pass_k 个通过即整行通过。
    返回 (是否通过, 聚合理由, usage 汇总, per_model 列表)。"""
    base = {k: v for k, v in row.items() if not k.startswith("_qc")}
    system = render_template(config.get("system_prompt", ""), base)
    user = render_template(config.get("user_prompt", ""), base)
    params = {**config.get("params", {}), "json_mode": True}
    retries = config.get("retries", 3)

    async def judge_one(mc: ModelConfig):
        async with user_sem:
            text, usage = await llm.chat(mc, system, user, params=params, retries=retries)
        verdict = _json.loads(text)
        if "pass" not in verdict:
            raise ValueError("质检判定未返回 pass 字段")
        return mc.id, bool(verdict["pass"]), str(verdict.get("reason") or "未通过质检"), usage

    results = await asyncio.gather(*[judge_one(mc) for mc in mcs])
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    per_model, n_pass, dissent = [], 0, []
    for mc_id, ok, reason, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        per_model.append({"model_config_id": mc_id, "pass": ok, "reason": reason})
        if ok:
            n_pass += 1
        else:
            dissent.append(reason)
    return n_pass >= pass_k, ("；".join(dissent) if dissent else "通过"), usage_total, per_model
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_qc_multi.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/nodes.py backend/tests/test_qc_multi.py
git commit -m "feat(qc): run_qc_judge_row 多模型 K-of-N 判定" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: runner._run_qc_node 多模型改造 + 指标/失败样本落库

**Files:**
- Modify: `backend/app/engine/runner.py:10`（import +QcMetric +QcFailure）、`:251-329`（`_run_qc_node`）
- Test: `backend/tests/test_qc.py`（更新现有；现有 test_qc.py 引用单模型 QC）

> 注意：先读 `backend/tests/test_qc.py` 现有用例，按新签名更新它们（旧用例用 `model_config_id` 单模型 → 改 `judge_model_ids=[id]` + `pass_k=1`，断言不变即可），再加新断言。

- [ ] **Step 1: 更新/新增测试**

在 `backend/tests/test_qc.py` 增加（多判定模型 + 首轮指标 + 失败样本落库）。沿用该文件已有的构造工作流/跑数 helper（读现有文件确认 helper 名）。核心新断言：
```python
async def test_qc_multi_model_metric_and_failures(auth_client, session_factory):
    # 构造：input -> llm_synth -> qc(judge_model_ids=[m1,m2], pass_k=2)，无 rescan 边
    # 准备数据让部分行 0/2 或 1/2 通过 -> 进 QcFailure；first_round_pass 写入 QcMetric
    # （沿用本文件既有的 mock llm + 建图 helper）
    ...
    from sqlalchemy import select
    from app.models import QcFailure, QcMetric
    async with session_factory() as s:
        m = (await s.execute(select(QcMetric))).scalars().all()
        f = (await s.execute(select(QcFailure))).scalars().all()
    assert m and m[0].total > 0                      # 首轮指标已落库
    assert all(0 <= x.first_round_pass <= x.total for x in m)
    # 不通过的样本进了 QcFailure，且 reasons_json 含 per_model
    if f:
        import json
        assert isinstance(json.loads(f[0].reasons_json), list)
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_qc.py -v`
Expected: FAIL（runner 仍按单 `model_config_id`；QcMetric/QcFailure 未写）

- [ ] **Step 3: 改 runner**

`backend/app/engine/runner.py` 第 10 行 import 追加 `QcFailure, QcMetric`：
```python
from app.models import (DatasetRow, ModelConfig, QcFailure, QcMetric, Run, RunLog,
                        RunNodeState, RunRow, WorkflowVersion)
```
把 `_run_qc_node`（251-329 行）替换为下面版本（改动点：判定模型列表解析+校验、judge_all 改多模型并带 per_model、首轮写 QcMetric、结束写 QcFailure）：
```python
async def _run_qc_node(session_factory, run_id, user_id, graph: Graph, node: Node, inputs,
                       user_sem, cancel_event):
    """质检节点：N 个判定模型 K-of-N 判定；不通过行带原因经 rescan 回扫重生，最多 max_rounds 轮。
    首轮通过数写 QcMetric；最终仍失败样本写 QcFailure；仅持久化最终通过行。"""
    cfg = node.config
    judge_ids = cfg.get("judge_model_ids") or (
        [cfg["model_config_id"]] if cfg.get("model_config_id") else [])
    pass_k = cfg.get("pass_k", 1)
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
        jmcs = [await s.get(ModelConfig, jid) for jid in judge_ids]
    if rec is not None and rec.status == "done":
        await _set_node_state(session_factory, run_id, node.id, status="done",
                              total=len(inputs), done=len(inputs), failed=0)
        return
    if not jmcs or any(m is None or m.user_id != user_id for m in jmcs):
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
                return await _cancellable(
                    nodes.run_qc_judge_row(cfg, row, jmcs, pass_k, user_sem), cancel_event)
        passed_, failed_ = [], []
        for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append(row)
            else:
                failed_.append({**row, "_qc_reason": reason, "_qc_per_model": per_model})
        return passed_, failed_

    try:
        passed, failed = await judge_all(inputs)
        async with session_factory() as s:        # 首轮指标落库
            s.add(QcMetric(run_id=run_id, node_id=node.id,
                           total=len(inputs), first_round_pass=len(passed)))
            await s.commit()
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
        if failed:                                # 最终仍失败样本落库
            async with session_factory() as s:
                for fr in failed:
                    sample = {k: v for k, v in fr.items() if not k.startswith("_qc")}
                    s.add(QcFailure(run_id=run_id, node_id=node.id,
                                    sample_json=_json.dumps(sample, ensure_ascii=False),
                                    reasons_json=_json.dumps(fr.get("_qc_per_model", []),
                                                             ensure_ascii=False)))
                await s.commit()
    except asyncio.CancelledError:
        return
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

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_qc.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_qc.py
git commit -m "feat(qc): 节点多模型判定 + 首轮指标/失败样本落库" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 质检指标/失败样本 API + 级联清理

**Files:**
- Modify: `backend/app/routers/runs.py`（import +QcFailure +QcMetric；+2 个 GET；delete_run 级联）、`backend/app/routers/admin.py`（delete_user 级联补表）
- Test: `backend/tests/test_qc_api.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_qc_api.py`：
```python
from app.models import QcFailure, QcMetric


async def _seed(session_factory, run_id):
    async with session_factory() as s:
        s.add(QcMetric(run_id=run_id, node_id="qc1", total=10, first_round_pass=6))
        s.add(QcFailure(run_id=run_id, node_id="qc1", sample_json='{"q":"x"}',
                        reasons_json='[{"model_config_id":2,"pass":false,"reason":"短"}]'))
        await s.commit()


async def test_qc_metrics_and_failures_endpoints(auth_client, session_factory):
    # auth_client 已登录 tester；造一个属于 tester 的 run
    from sqlalchemy import select
    from app.models import Run, User
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        run = Run(user_id=uid, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run); await s.commit(); run_id = run.id
    await _seed(session_factory, run_id)
    metrics = (await auth_client.get(f"/api/runs/{run_id}/qc-metrics")).json()
    assert metrics[0]["first_round_pass"] == 6 and abs(metrics[0]["first_round_rate"] - 0.6) < 1e-6
    failures = (await auth_client.get(f"/api/runs/{run_id}/qc-failures")).json()
    assert failures[0]["sample"]["q"] == "x" and failures[0]["reasons"][0]["reason"] == "短"


async def test_qc_endpoints_reject_foreign_run(auth_client, client, session_factory):
    from sqlalchemy import select
    from app.models import Run, User
    await client.post("/api/auth/login", json={"username": "stranger"})
    async with session_factory() as s:
        sid = (await s.execute(select(User).where(User.username == "stranger"))).scalar_one().id
        run = Run(user_id=sid, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run); await s.commit(); rid = run.id
    assert (await auth_client.get(f"/api/runs/{rid}/qc-metrics")).status_code == 404
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_qc_api.py -v`
Expected: FAIL（404/未实现）

- [ ] **Step 3: 实现 API + 级联**

`backend/app/routers/runs.py` 第 16-18 行 import 追加 `QcFailure, QcMetric`：
```python
from app.models import (Dataset, ModelConfig, QcFailure, QcMetric, Run, RunLog, RunNodeState,
                        RunRow, User, Workflow, WorkflowVersion)
```
在 `run_logs` 端点之后新增两个端点：
```python
@router.get("/{run_id}/qc-metrics")
async def run_qc_metrics(run_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    rows = (await session.execute(
        select(QcMetric).where(QcMetric.run_id == run_id).order_by(QcMetric.id))).scalars().all()
    return [{"node_id": m.node_id, "total": m.total, "first_round_pass": m.first_round_pass,
             "first_round_rate": (m.first_round_pass / m.total) if m.total else 0.0} for m in rows]


@router.get("/{run_id}/qc-failures")
async def run_qc_failures(run_id: int, node_id: str | None = None, limit: int = 200,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    stmt = select(QcFailure).where(QcFailure.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(QcFailure.node_id == node_id)
    rows = (await session.execute(stmt.order_by(QcFailure.id).limit(limit))).scalars().all()
    return [{"node_id": f.node_id, "sample": json.loads(f.sample_json),
             "reasons": json.loads(f.reasons_json), "created_at": f.created_at.isoformat()}
            for f in rows]
```
在 `delete_run` 的级联块（现有 `sa_delete(RunLog)` 行后）追加两行：
```python
    await session.execute(sa_delete(QcMetric).where(QcMetric.run_id == run_id))
    await session.execute(sa_delete(QcFailure).where(QcFailure.run_id == run_id))
```
`backend/app/routers/admin.py` 的 `delete_user` 级联：找到删 `RunLog` 的位置（按 run_id 集合删），在同处追加对 `QcMetric`/`QcFailure` 的删除（先 `from app.models import QcFailure, QcMetric`，按该文件既有的「先查该用户所有 run_id 再 in_ 删」模式补两表）。

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_qc_api.py tests/test_admin.py -v`
Expected: PASS（admin 级联回归不破）

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/runs.py backend/app/routers/admin.py backend/tests/test_qc_api.py
git commit -m "feat(qc): 质检指标/失败样本 API + 级联清理" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: create_run 校验 QC 多判定模型

**Files:**
- Modify: `backend/app/routers/runs.py:60-65`（create_run 的节点资源校验）
- Test: `backend/tests/test_runs_api.py`（补一条）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_runs_api.py` 增加（沿用该文件既有建图 helper）：
```python
async def test_create_run_validates_qc_judge_models(auth_client, session_factory):
    # 造一个 qc 节点 config={"judge_model_ids":[999999], "pass_k":1}（不存在的模型）
    # 期望 create_run 返回 422，detail 含「模型」
    ...
    r = await auth_client.post("/api/runs", json={"workflow_id": wf_id})
    assert r.status_code == 422
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_runs_api.py -k judge_models -v`
Expected: FAIL（校验只看 `model_config_id`，judge_model_ids 漏过）

- [ ] **Step 3: 改校验**

`backend/app/routers/runs.py` create_run 中 `if n.type in ("llm_synth", "qc"):` 块改为分别处理：
```python
        if n.type == "llm_synth":
            mc_id = n.config.get("model_config_id")
            mc = await session.get(ModelConfig, mc_id) if mc_id else None
            if mc is None or mc.user_id != user.id:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择有效的模型配置")
        if n.type == "qc":
            ids = n.config.get("judge_model_ids") or (
                [n.config["model_config_id"]] if n.config.get("model_config_id") else [])
            if not ids:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择判定模型")
            for jid in ids:
                mc = await session.get(ModelConfig, jid)
                if mc is None or mc.user_id != user.id:
                    raise HTTPException(status_code=422, detail=f"节点 {n.id}: 判定模型无效")
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_runs_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/runs.py backend/tests/test_runs_api.py
git commit -m "feat(qc): create_run 校验多判定模型归属" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: CLI node set 多判定模型 + pass_k

**Files:**
- Modify: `backend/app/cli.py`（`cmd_node_set` 加 `judge_models` / `pass_k` 键）
- Test: `backend/tests/test_cli.py`（补一条）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_cli.py` 增加（沿用该文件既有的 CLI 调用 helper）：
```python
def test_node_set_judge_models(cli_env):
    # gf node set qc1 judge_models=<m1>,<m2> pass_k=2
    # 断言节点 config 出现 judge_model_ids=[m1,m2] 与 pass_k=2
    ...
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_cli.py -k judge_models -v`
Expected: FAIL（`未知配置键 judge_models`）

- [ ] **Step 3: 改 cmd_node_set**

`backend/app/cli.py` 的 `cmd_node_set` 循环里，在 `elif k == "max_rounds":` 之前插入：
```python
        elif k == "judge_models":
            cfg["judge_model_ids"] = [cli.resolve("models", r) for r in v.split(",") if r]
        elif k == "pass_k":
            cfg["pass_k"] = int(v)
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat(qc): gf node set 支持 judge_models/pass_k" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 前端 QcForm 多选 + 失败样本展示 + 类型

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（QcForm）、`frontend/src/pages/RunDetailPage.tsx`（质检失败样本区）、`frontend/src/api/types.ts`
- Test: `cd frontend; npm run build`

- [ ] **Step 1: 加类型**

`frontend/src/api/types.ts` 末尾追加：
```typescript
export interface QcMetricEntry { node_id: string; total: number; first_round_pass: number; first_round_rate: number }
export interface QcFailureEntry { node_id: string; sample: Record<string, any>; reasons: { model_config_id: number; pass: boolean; reason: string }[]; created_at: string }
```

- [ ] **Step 2: QcForm 改多选 + pass_k**

`frontend/src/canvas/forms/NodeConfigForm.tsx` 的 `QcForm` 里，把「判定模型」`Field` 替换为多选 + K：
```tsx
      <Field label="判定模型（多选，N 个模型同提示词判定）">
        <Select mode="multiple" style={{ width: '100%' }}
                value={config.judge_model_ids ?? (config.model_config_id ? [config.model_config_id] : [])}
                onChange={(v) => patch({ judge_model_ids: v })}
                options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))} />
      </Field>
      <Field label="至少通过数 K（≥K 个模型通过即输出）">
        <InputNumber min={1} value={config.pass_k ?? 1} onChange={(v) => patch({ pass_k: v ?? 1 })} />
      </Field>
```
（`InputNumber` 已在文件顶部 antd import 中。）

- [ ] **Step 3: RunDetailPage 加「质检失败样本」区**

`frontend/src/pages/RunDetailPage.tsx`：import 追加 `QcFailureEntry`；在组件内加 state 与拉取（非活跃时拉），并在「运行日志」Card 之后渲染一个折叠表：
```tsx
  const [qcFailures, setQcFailures] = useState<QcFailureEntry[]>([])
  useEffect(() => {
    if (!run || isActive) return
    void api.get<QcFailureEntry[]>(`/api/runs/${id}/qc-failures`).then(setQcFailures)
  }, [run?.status, id, isActive])
```
在日志 Card 后：
```tsx
      {!isActive && qcFailures.length > 0 && (
        <Card size="small" title={`质检失败样本（${qcFailures.length}）`} style={{ marginBottom: 16 }}
              extra={<Button size="small" onClick={() => {
                const blob = new Blob([JSON.stringify(qcFailures, null, 2)], { type: 'application/json' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a'); a.href = url; a.download = `run${id}_qc_failures.json`; a.click()
                URL.revokeObjectURL(url)
              }}>下载</Button>}>
          <Table rowKey={(_, i) => String(i)} dataSource={qcFailures} size="small"
                 pagination={{ pageSize: 10 }}
                 columns={[
                   { title: '样本', dataIndex: 'sample', ellipsis: true,
                     render: (v: object) => JSON.stringify(v) },
                   { title: '各模型理由', dataIndex: 'reasons',
                     render: (rs: QcFailureEntry['reasons']) =>
                       rs.map((r) => `${r.pass ? '✓' : '✗'} ${r.reason}`).join('；') },
                 ]} />
        </Card>
      )}
```

- [ ] **Step 4: 构建验证**

Run: `cd frontend; npm run build`
Expected: 构建成功，无类型错误

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/pages/RunDetailPage.tsx frontend/src/api/types.ts
git commit -m "feat(qc): 前端 QcForm 多选判定模型 + 失败样本展示" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# F2 · 上下文 Compactor（共享模块）

## Task 8: OpenRouter 模型窗口查询（model_meta.py）

**Files:**
- Create: `backend/app/agent/model_meta.py`
- Test: `backend/tests/test_model_meta.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_model_meta.py`：
```python
import app.agent.model_meta as mm


async def test_window_from_cache(monkeypatch):
    mm._CACHE.clear()
    mm._CACHE.update({"openai/gpt-x": 200000})
    assert await mm.model_window("openai/gpt-x") == 200000


async def test_window_fallback_when_unknown(monkeypatch):
    mm._CACHE.clear()
    async def fake_fetch():
        return {}                      # 拉取成功但无该模型
    monkeypatch.setattr(mm, "_fetch_models", fake_fetch)
    assert await mm.model_window("nonexistent/model") == mm.DEFAULT_WINDOW


async def test_window_fallback_when_fetch_fails(monkeypatch):
    mm._CACHE.clear()
    async def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(mm, "_fetch_models", boom)
    assert await mm.model_window("any") == mm.DEFAULT_WINDOW
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_model_meta.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`backend/app/agent/model_meta.py`：
```python
"""按模型名查 OpenRouter 上下文窗口（进程内缓存，查不到回退默认）。公开端点、不带任何 key。"""
import httpx

DEFAULT_WINDOW = 128_000
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_CACHE: dict[str, int] = {}
_FETCHED = False


async def _fetch_models() -> dict[str, int]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return {m["id"]: int(m.get("context_length") or 0) for m in data if m.get("context_length")}


async def model_window(model_name: str) -> int:
    """返回模型上下文窗口 token 数；首次调用拉取并缓存，失败/查不到回退 DEFAULT_WINDOW。"""
    global _FETCHED
    if model_name in _CACHE:
        return _CACHE[model_name]
    if not _FETCHED:
        _FETCHED = True
        try:
            _CACHE.update(await _fetch_models())
        except Exception:
            pass
    return _CACHE.get(model_name, DEFAULT_WINDOW)
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_model_meta.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/model_meta.py backend/tests/test_model_meta.py
git commit -m "feat(agent): OpenRouter 模型窗口查询 model_meta" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Compactor 压缩逻辑（compactor.py）

**Files:**
- Create: `backend/app/agent/compactor.py`
- Test: `backend/tests/test_compactor.py`（新建）

> 关键事实：pydantic-ai 1.107 的消息类型在 `pydantic_ai.messages`：`ModelRequest`（含 `parts`，其中 `UserPromptPart`/`ToolReturnPart`）、`ModelResponse`（含 `parts`，其中 `TextPart`/`ToolCallPart`）。测试用真实消息对象构造，`compact` 不依赖具体模型调用（compactor LLM 调用通过注入的 `summarize` 回调，便于测试）。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_compactor.py`：
```python
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
import app.agent.compactor as cp


def _user(text):
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text):
    return ModelResponse(parts=[TextPart(content=text)])


async def test_below_threshold_passthrough():
    history = [_user("目标"), _assistant("做了点事")]
    out = await cp.maybe_compact(history, compactor_mc=None, running_mc=None,
                                 window=1_000_000, summarize=None)
    assert out is history                          # 未达 75%，原样返回


async def test_compaction_protects_head_and_tail():
    async def fake_summarize(text):
        return "【已完成】A【待完成】B"
    history = [_user("总目标")] + [_assistant(f"中间{i}") for i in range(50)] + [_user("最近一步")]
    out = await cp.maybe_compact(history, compactor_mc=object(), running_mc=object(),
                                 window=10, summarize=fake_summarize)  # window 极小 -> 必触发
    assert len(out) < len(history)                 # 确实压缩了
    assert out[0] is history[0]                    # 首条（目标）逐字保留
    assert out[-1] is history[-1]                  # 尾条逐字保留
    joined = "".join(p.content for m in out for p in m.parts if hasattr(p, "content"))
    assert "已完成" in joined                       # 结构化摘要插入


async def test_compaction_skips_on_summarize_failure():
    async def boom(text):
        raise RuntimeError("llm down")
    history = [_user("目标")] + [_assistant(f"x{i}") for i in range(50)]
    out = await cp.maybe_compact(history, compactor_mc=object(), running_mc=object(),
                                 window=10, summarize=boom)
    assert out is history                          # 压缩失败 -> 用原历史
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_compactor.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`backend/app/agent/compactor.py`：
```python
"""上下文 Compactor：在 agent.run 前压缩过长历史。规则=工具输出只留结果 + 首尾保护 + 结构化摘要。
所有 Agent 角色统一复用。不依赖已废弃的 history_processor 构造参数。"""
from pydantic_ai.messages import ModelRequest, TextPart

from app.agent.factory import create_model
from app.agent.model_meta import model_window

KEEP_TAIL = 6            # 末尾逐字保留的消息条数
COMPACT_RATIO = 0.75


def estimate_tokens(history: list) -> int:
    """字符启发式：所有 part.content 字符数 / 3（中英混合粗估）。"""
    chars = 0
    for m in history:
        for p in getattr(m, "parts", []):
            c = getattr(p, "content", None)
            if isinstance(c, str):
                chars += len(c)
    return chars // 3


def _strip_to_text(history: list) -> str:
    """把中间段消息拍平成纯文本（工具调用/返回只留其文本结果），喂给 compactor 总结。"""
    lines = []
    for m in history:
        for p in getattr(m, "parts", []):
            c = getattr(p, "content", None)
            if isinstance(c, str) and c.strip():
                lines.append(c.strip())
    return "\n".join(lines)


async def _default_summarize(compactor_mc, text: str) -> str:
    from app.services import llm
    system = ("你是上下文压缩器。把下面的 Agent 工作历史压缩成简洁结构化摘要，"
              "必须包含两节：【已完成】列已达成的目标/产出；【待完成】列尚未完成的任务/已知问题。"
              "只保留对继续推进有用的结论，删除寒暄与中间过程。")
    out, _usage = await llm.chat(compactor_mc, system, text, params={}, retries=2)
    return out


async def maybe_compact(history: list, *, compactor_mc, running_mc, window: int | None = None,
                        summarize=None, emit=None) -> list:
    """达 75% 窗口才压缩；否则原样返回。summarize 可注入（测试用），默认走 compactor LLM。
    压缩失败时返回原历史。"""
    if window is None:
        window = await model_window(running_mc.model_name)
    if estimate_tokens(history) < COMPACT_RATIO * window:
        return history
    if len(history) <= KEEP_TAIL + 1:
        return history
    head, middle, tail = history[:1], history[1:-KEEP_TAIL], history[-KEEP_TAIL:]
    if not middle:
        return history
    if summarize is None:
        async def summarize(text):
            return await _default_summarize(compactor_mc, text)
    try:
        if emit:
            await emit("compacting", {"before": len(history)})
        summary = await summarize(_strip_to_text(middle))
    except Exception:
        return history
    summary_msg = ModelRequest(parts=[TextPart(content=f"[上下文摘要]\n{summary}")])
    return head + [summary_msg] + tail


def resolve_compactor_model(models: dict):
    """models: {role: ModelConfig 或 pydantic-ai Model}。返回 compactor 模型（默认复用 coordinator）。
    非 ModelConfig（测试用 Model 实例）一律返回 None，调用方据此跳过压缩。"""
    from app.models import ModelConfig
    mc = models.get("compactor") or models.get("coordinator")
    return mc if isinstance(mc, ModelConfig) else None
```

> 说明：`ModelRequest(parts=[TextPart(...)])` 仅作摘要载体；若 1.107 校验 `ModelRequest` 不接受 `TextPart`，实现者改用 `UserPromptPart(content=...)`（按安装版 `pydantic_ai.messages` 实际类型微调；测试断言只检查 `.content` 文本，不锁定 part 类型）。`create_model` 已 import 备 `_default_summarize` 不需要——`llm.chat` 直接收 ModelConfig，无需构造 pydantic-ai model。可删除未用 import `create_model`。

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_compactor.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/compactor.py backend/tests/test_compactor.py
git commit -m "feat(agent): 上下文 Compactor 压缩逻辑" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 把 compaction 接入 agent.run seam + compactor 角色

**Files:**
- Modify: `backend/app/agent/system.py`（run_turn / manager / 注入 compactor）、`backend/app/agent/orchestrator.py`（worker seam）、`backend/app/routers/agent.py`（compactor 角色默认）、`backend/app/agent/turns.py`（解析 compactor 模型）
- Test: `backend/tests/test_agent_sessions.py`（补 compactor 默认）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_sessions.py`（或现有 agent 会话测试文件）增加：
```python
async def test_session_defaults_compactor_to_coordinator(auth_client, session_factory):
    import json
    from sqlalchemy import select
    from app.models import AgentSession, ModelConfig, User
    # 造一个属于 tester 的模型配置
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        mc = ModelConfig(user_id=uid, name="m", base_url="http://x", api_key_enc="")
        s.add(mc); await s.commit(); mid = mc.id
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mid})
    assert r.status_code == 200
    async with session_factory() as s:
        sess = (await s.execute(select(AgentSession).order_by(AgentSession.id.desc()))).scalars().first()
        models = json.loads(sess.models_json)
    assert models["compactor"] == mid           # compactor 默认 = coordinator 模型
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_agent_sessions.py -k compactor -v`
Expected: FAIL（models_json 无 compactor 键）

- [ ] **Step 3: compactor 角色默认 + seam 接入**

`backend/app/routers/agent.py` create_session 中（`models = body.models or {...}` 之后、`_check_models` 之前）补：
```python
    models.setdefault("compactor", models["coordinator"])
```
`_check_models` 改为校验全部所给键（而非仅 ROLES）：
```python
async def _check_models(models: dict, user: User, session: AsyncSession) -> None:
    for role in ("coordinator", "manager", "worker", "compactor"):
        mc = await session.get(ModelConfig, models.get(role) or 0)
        if mc is None or mc.user_id != user.id:
            raise HTTPException(status_code=422, detail=f"角色 {role} 的模型配置无效")
```
`backend/app/agent/system.py`：`AgentSystem.__init__` 计算并存 compactor 模型与一个绑定 helper；在 `run_turn` 里 `agent.run` 前压缩 history：
```python
from app.agent.compactor import maybe_compact, resolve_compactor_model
```
`__init__` 末尾追加：
```python
        self._compactor_mc = resolve_compactor_model(models)
```
`run_turn` 改为：
```python
    async def run_turn(self, text: str, history: list) -> tuple[list, str]:
        tools = [self.execute_task_with_manager, self.execute_task_with_worker]
        tools += self._make_tools(self._main_state)
        coord = self.models["coordinator"]
        if self._compactor_mc is not None:
            history = await maybe_compact(history, compactor_mc=self._compactor_mc,
                                          running_mc=coord, emit=self.emit)
        agent = create_agent(coord, tools, get_coordinator_system_prompt(self.skills_manager))
        result = await agent.run(text, message_history=history,
                                 event_stream_handler=self._on_stream if self.emit else None)
        return result.all_messages(), str(result.output or "")
```
（manager 与 worker seam 可同法在各自 `agent.run` 前调用 `maybe_compact`；本任务最小落地 coordinator 主循环——goal 模式即在此。manager/worker 接入作为同文件同模式的补充，若时间允许一并加，断言不变。）
`backend/app/agent/turns.py` `_run_turn` 读取 models 后，`compactor` 缺失时回退 coordinator（兼容旧会话）：在 `models = {role: await s.get(...) ...}` 之后追加：
```python
            models.setdefault("compactor", models.get("coordinator"))
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_agent_sessions.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/agent.py backend/app/agent/system.py backend/app/agent/turns.py backend/tests/test_agent_sessions.py
git commit -m "feat(agent): compactor 角色默认 + run_turn 前压缩接入" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# F3 · 目标优化模式

## Task 11: RunManager 完成事件 + wait + _finish publish

**Files:**
- Modify: `backend/app/engine/manager.py`（done_events + wait）、`backend/app/engine/runner.py`（_finish 末尾 publish）
- Test: `backend/tests/test_run_manager_wait.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_run_manager_wait.py`：
```python
import asyncio
from app.engine.manager import RunManager


async def test_wait_fires_on_completion(monkeypatch):
    m = RunManager()
    # 用一个立即完成的假 execute_run
    import app.engine.manager as mod
    async def fake_exec(run_id, sf, sem, ev):
        await asyncio.sleep(0)
    monkeypatch.setattr(mod, "execute_run", fake_exec)
    m.submit(123, user_id=1, capacity=2, session_factory=None)
    await asyncio.wait_for(m.wait(123), timeout=1)   # 不挂起即通过


async def test_wait_unknown_run_returns_immediately():
    m = RunManager()
    await asyncio.wait_for(m.wait(999), timeout=1)
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_run_manager_wait.py -v`
Expected: FAIL（`RunManager` 无 `wait`）

- [ ] **Step 3: 实现**

`backend/app/engine/manager.py`：`__init__` 加 `self.done_events: dict[int, asyncio.Event] = {}`；`submit` 改为创建 done 事件并在完成回调里先 set 再 cleanup：
```python
    def submit(self, run_id: int, user_id: int, capacity: int,
               session_factory) -> None:
        ev = asyncio.Event()
        done = asyncio.Event()
        self.cancel_events[run_id] = ev
        self.done_events[run_id] = done
        task = asyncio.create_task(
            execute_run(run_id, session_factory, self.user_sem(user_id, capacity), ev))
        self.tasks[run_id] = task

        def _on_done(_):
            done.set()
            self._cleanup(run_id)
        task.add_done_callback(_on_done)

    def _cleanup(self, run_id: int) -> None:
        self.cancel_events.pop(run_id, None)
        self.done_events.pop(run_id, None)
        self.tasks.pop(run_id, None)

    async def wait(self, run_id: int) -> None:
        """等待某次运行到达终态。未知/已结束 run 立即返回。"""
        done = self.done_events.get(run_id)
        if done is not None:
            await done.wait()
```
`backend/app/engine/runner.py` 的 `_finish` 末尾（`_log` 之后）补 SSE 通知（前端目标面板与运行列表实时刷新）：在 `runner.py` 顶部 import `from app.events import publish`；`_finish` 改为先取 `user_id` 再 publish：
```python
async def _finish(session_factory, run_id, status):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        user_id = run.user_id
        sums = (await s.execute(
            select(func.coalesce(func.sum(RunRow.prompt_tokens), 0),
                   func.coalesce(func.sum(RunRow.completion_tokens), 0))
            .where(RunRow.run_id == run_id, RunRow.status == "done"))).one()
        run.stats_json = json.dumps({"prompt_tokens": sums[0], "completion_tokens": sums[1]})
        run.status = status
        run.finished_at = _now()
        await s.commit()
    await _log(session_factory, run_id, "",
               f"运行结束：{status}（prompt={sums[0]} completion={sums[1]}）")
    publish(user_id, "run", run_id)
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_run_manager_wait.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/manager.py backend/app/engine/runner.py backend/tests/test_run_manager_wait.py
git commit -m "feat(run): RunManager 完成事件 + wait + _finish publish" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: 跑数服务 + 指标/失败样本/阈值 工具函数

**Files:**
- Create: `backend/app/services/run_service.py`（enqueue_run + 指标/失败样本读取 + 阈值解析）
- Test: `backend/tests/test_run_service.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_run_service.py`：
```python
import app.services.run_service as rs


def test_parse_threshold():
    assert rs.parse_threshold("把首轮质检通过率提升到 90% 以上") == 0.9
    assert rs.parse_threshold("达到 0.85") == 0.85
    assert rs.parse_threshold("把数据清洗干净") is None


async def test_first_round_rate_aggregates(session_factory):
    from app.models import QcMetric
    async with session_factory() as s:
        s.add(QcMetric(run_id=7, node_id="a", total=10, first_round_pass=6))
        s.add(QcMetric(run_id=7, node_id="b", total=10, first_round_pass=8))
        await s.commit()
    rate = await rs.first_round_rate(session_factory, 7)
    assert abs(rate - 0.7) < 1e-6                 # (6+8)/(10+10)


async def test_first_round_rate_none_when_no_metric(session_factory):
    assert await rs.first_round_rate(session_factory, 999) is None
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_run_service.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`backend/app/services/run_service.py`：
```python
"""目标循环用的跑数服务：入队一次运行、读首轮指标、抽样失败样本、解析文本阈值。"""
import json
import re

from sqlalchemy import func, select

from app.engine.graph import parse_graph, validate_graph
from app.engine.manager import manager
from app.models import QcFailure, QcMetric, Run, User, Workflow, WorkflowVersion

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_FRAC_RE = re.compile(r"\b(0?\.\d+|1\.0)\b")


def parse_threshold(text: str) -> float | None:
    """从文本目标解析阈值：百分比 90% -> 0.9，小数 0.85 -> 0.85，解析不到 -> None。"""
    m = _PCT_RE.search(text)
    if m:
        return float(m.group(1)) / 100
    m = _FRAC_RE.search(text)
    if m:
        return float(m.group(1))
    return None


def workflow_has_qc(graph) -> bool:
    return any(n.type == "qc" for n in graph.nodes)


async def enqueue_run(session_factory, user_id: int, workflow_id: int) -> int:
    """快照工作流图为版本并入队一次运行，返回 run_id（不阻塞等待）。"""
    async with session_factory() as s:
        wf = await s.get(Workflow, workflow_id)
        if wf is None or wf.user_id != user_id:
            raise ValueError("工作流不存在")
        validate_graph(parse_graph(wf.graph_json))
        max_ver = (await s.execute(select(func.max(WorkflowVersion.version)).where(
            WorkflowVersion.workflow_id == workflow_id))).scalar() or 0
        ver = WorkflowVersion(workflow_id=workflow_id, version=max_ver + 1, graph_json=wf.graph_json)
        s.add(ver)
        await s.flush()
        run = Run(user_id=user_id, workflow_id=workflow_id, workflow_version_id=ver.id)
        s.add(run)
        await s.commit()
        run_id = run.id
        user = await s.get(User, user_id)
        capacity = user.max_llm_concurrency
    manager.submit(run_id, user_id, capacity, session_factory)
    return run_id


async def first_round_rate(session_factory, run_id: int) -> float | None:
    """聚合该运行所有 QC 节点的首轮通过率；无指标返回 None。"""
    async with session_factory() as s:
        rows = (await s.execute(select(QcMetric).where(QcMetric.run_id == run_id))).scalars().all()
    total = sum(m.total for m in rows)
    if not rows or total == 0:
        return None
    return sum(m.first_round_pass for m in rows) / total


async def sample_failures(session_factory, run_id: int, n: int = 20) -> list[dict]:
    """抽样最多 n 条质检失败样本（含各模型理由）。"""
    async with session_factory() as s:
        rows = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id)
                                .order_by(QcFailure.id).limit(n))).scalars().all()
    return [{"sample": json.loads(f.sample_json), "reasons": json.loads(f.reasons_json)}
            for f in rows]
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_run_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/run_service.py backend/tests/test_run_service.py
git commit -m "feat(goal): 跑数服务 + 首轮指标/失败样本/阈值解析" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 目标优化循环（goal_loop.py + turns 接入）

**Files:**
- Create: `backend/app/agent/goal_loop.py`（轮次提示构造 + 跳出判定纯函数）
- Modify: `backend/app/agent/turns.py`（`submit_goal` + `_run_goal`）、`backend/app/config.py`（goal_no_improve_k）
- Test: `backend/tests/test_goal_loop.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_goal_loop.py`：
```python
import app.agent.goal_loop as gl


def test_should_stop_threshold_hit():
    s = gl.decide(metric=0.92, threshold=0.9, best=0.8, no_improve=0, no_improve_k=2)
    assert s.stop and s.success and "达成" in s.reason


def test_should_stop_no_improve():
    s = gl.decide(metric=0.5, threshold=0.9, best=0.6, no_improve=1, no_improve_k=2)
    assert s.stop and not s.success and "无提升" in s.reason


def test_continue_when_improving():
    s = gl.decide(metric=0.7, threshold=0.9, best=0.6, no_improve=0, no_improve_k=2)
    assert not s.stop and s.new_best == 0.7 and s.new_no_improve == 0


def test_no_threshold_never_threshold_stops():
    s = gl.decide(metric=0.99, threshold=None, best=0.5, no_improve=0, no_improve_k=2)
    assert not s.stop                             # 无阈值不靠指标停（靠 DONE/手动）


def test_round_prompt_includes_metric_and_failures_and_distill():
    p = gl.build_round_prompt("目标X", metric=0.6, failures=[{"sample": {"q": "a"}, "reasons": []}], run_id=5)
    assert "0.6" in p or "60" in p
    assert "凝练" in p and "打补丁" in p           # 凝练经验、非打补丁
    assert "q" in p
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_goal_loop.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现纯函数模块 + 接入 turns**

`backend/app/agent/goal_loop.py`：
```python
"""目标优化循环的纯函数：跳出判定与轮次提示构造（便于单测，无 I/O）。"""
import json
from dataclasses import dataclass


@dataclass
class Decision:
    stop: bool
    success: bool
    reason: str
    new_best: float
    new_no_improve: int


def decide(*, metric, threshold, best, no_improve, no_improve_k) -> Decision:
    """根据本轮实测指标决定是否跳出。metric/threshold 可为 None。"""
    if metric is None:                           # 本轮没算出指标（如跑数失败）：计入无提升
        no_improve += 1
        if threshold is not None and no_improve >= no_improve_k:
            return Decision(True, False, f"连续 {no_improve} 轮无提升，停止（最佳 {best:.1%}）", best, no_improve)
        return Decision(False, False, "本轮无有效指标", best, no_improve)
    if threshold is not None and metric >= threshold:
        return Decision(True, True, f"✅ 目标达成：首轮质检通过率 {metric:.1%} ≥ {threshold:.1%}", max(best, metric), 0)
    if metric > best:
        return Decision(False, False, "指标提升", metric, 0)
    no_improve += 1
    if threshold is not None and no_improve >= no_improve_k:
        return Decision(True, False, f"连续 {no_improve} 轮无提升，停止（最佳 {best:.1%}）", best, no_improve)
    return Decision(False, False, "指标未提升", best, no_improve)


def build_round_prompt(goal_text: str, metric, failures: list, run_id: int) -> str:
    metric_str = "（首轮尚无指标）" if metric is None else f"{metric:.1%}"
    fail_str = json.dumps(failures, ensure_ascii=False, indent=2) if failures else "（无失败样本）"
    return (f"[目标]\n{goal_text}\n\n"
            f"[上一轮运行 #{run_id} 实测：首轮质检通过率 = {metric_str}]\n\n"
            f"[真实质检失败样本抽样（含各判定模型理由）]\n{fail_str}\n\n"
            "请先**凝练通用经验**：从这些失败样本中归纳出可推广的规律（而不是针对单条样本打补丁），"
            "再据此用 gf 命令改进当前工作流的提示词/参数（必要时调整链路）。改完即结束本回合，"
            "系统会自动跑数并把新指标喂给你。仍需继续时回复末尾输出 "
            "`<!-- REDLOTUS_GOAL:CONTINUE -->`；若判断目标不可达请输出 `<!-- REDLOTUS_GOAL:DONE -->`。")


def first_round_prompt(goal_text: str) -> str:
    return (f"[目标]\n{goal_text}\n\n"
            "这是目标优化模式第一轮。请先用 gf 查看当前工作流结构与质检节点，"
            "凝练你对如何达成目标的初步判断，再改进提示词/参数。改完结束回合，系统会自动跑数。"
            "回复末尾输出 `<!-- REDLOTUS_GOAL:CONTINUE -->`。")
```
`backend/app/config.py` 在 `agent_goal_max_rounds` 后加：
```python
    goal_no_improve_k: int = 2  # 目标模式：连续无提升轮数早停阈值
```
`backend/app/agent/turns.py` 增加 goal 提交与循环（与 `_run_turn` 同结构，区别是：循环触发跑数 + await 完成 + 算指标 + decide）。在 `AgentTurnManager` 加：
```python
    def submit_goal(self, session_id: int, user_id: int, workflow_id: int, goal_text: str) -> None:
        self.stop_flags.discard(session_id)
        task = asyncio.create_task(self._run_goal(session_id, user_id, workflow_id, goal_text))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    async def _run_goal(self, session_id, user_id, workflow_id, goal_text):
        from app.agent import goal_loop as gl
        from app.engine.manager import manager
        from app.services import run_service as rs
        sf = get_session_factory()
        async with sf() as s:
            sess = await s.get(AgentSession, session_id)
            history = ModelMessagesTypeAdapter.validate_json(sess.history_json)
            models = {role: await s.get(ModelConfig, mid)
                      for role, mid in json.loads(sess.models_json).items()}
            models.setdefault("compactor", models.get("coordinator"))
            user = await s.get(User, user_id)
            username = user.username
        emit = self._make_emit(session_id, user_id)
        EMIT.set(emit)
        system = AgentSystem(models=models, workdir=session_dir(username, session_id),
                             confirm_delete=False, emit=emit)
        threshold = rs.parse_threshold(goal_text)
        best, no_improve, round_i = -1.0, 0, 0
        input_text = gl.first_round_prompt(goal_text)
        try:
            while True:
                history, output = await system.run_turn(input_text, history)
                signal, cleaned = parse_goal(output)
                await self._add_message(session_id, user_id, "assistant", {"text": cleaned})
                if signal == "DONE" or session_id in self.stop_flags:
                    break
                run_id = await rs.enqueue_run(sf, user_id, workflow_id)
                round_i += 1
                publish(user_id, "agent", session_id, kind="goal_round", data=round_i)
                await manager.wait(run_id)
                metric = await rs.first_round_rate(sf, run_id)
                publish(user_id, "agent", session_id, kind="goal_metric",
                        data={"round": round_i, "metric": metric, "run_id": run_id})
                d = gl.decide(metric=metric, threshold=threshold, best=best,
                              no_improve=no_improve, no_improve_k=settings.goal_no_improve_k)
                best, no_improve = d.new_best, d.new_no_improve
                if d.stop:
                    await self._add_message(session_id, user_id, "assistant", {"text": d.reason})
                    break
                if round_i >= settings.agent_goal_max_rounds:
                    await self._add_message(session_id, user_id, "assistant",
                                            {"text": f"已达轮数兜底上限（{settings.agent_goal_max_rounds}）"})
                    break
                failures = await rs.sample_failures(sf, run_id, n=20)
                input_text = gl.build_round_prompt(goal_text, metric, failures, run_id)
        except Exception as e:
            await self._add_message(session_id, user_id, "assistant", {"text": f"目标模式出错: {e}"})
        finally:
            async with sf() as s:
                sess = await s.get(AgentSession, session_id)
                if sess is not None:
                    sess.history_json = ModelMessagesTypeAdapter.dump_json(history).decode()
                    sess.status = "idle"
                    await s.commit()
            publish(user_id, "agent", session_id, kind="turn_done")
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_goal_loop.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/goal_loop.py backend/app/agent/turns.py backend/app/config.py backend/tests/test_goal_loop.py
git commit -m "feat(goal): 目标优化 while 循环（事件驱动跑数+凝练经验+跳出判定）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: 目标启动 API（含无 QC 节点前置校验）

**Files:**
- Modify: `backend/app/routers/agent.py`（`POST /sessions/{sid}/goal`）
- Test: `backend/tests/test_goal_api.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_goal_api.py`：
```python
async def test_goal_rejects_workflow_without_qc(auth_client, session_factory):
    # 造一个属于 tester、无 qc 节点的工作流；造一个 session
    # POST /api/agent/sessions/{sid}/goal {workflow_id, goal_text} -> 422 含「质检」
    ...
    r = await auth_client.post(f"/api/agent/sessions/{sid}/goal",
                               json={"workflow_id": wf_id, "goal_text": "提升到 90%"})
    assert r.status_code == 422 and "质检" in r.json()["detail"]
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_goal_api.py -v`
Expected: FAIL（端点不存在）

- [ ] **Step 3: 实现端点**

`backend/app/routers/agent.py` 增加（import `from app.engine.graph import parse_graph`、`from app.services.run_service import workflow_has_qc`、`from app.models import Workflow` 已有）：
```python
class GoalIn(BaseModel):
    workflow_id: int
    goal_text: str


@router.post("/sessions/{sid}/goal")
async def start_goal(sid: int, body: GoalIn, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    if sess.status == "running":
        raise HTTPException(status_code=409, detail="回合进行中")
    text = body.goal_text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="目标不能为空")
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    if not workflow_has_qc(parse_graph(wf.graph_json)):
        raise HTTPException(status_code=422, detail="目标工作流需包含质检节点才能度量首轮质检通过率")
    await _check_models(json.loads(sess.models_json), user, session)
    session.add(AgentMessage(session_id=sid, role="user",
                             content_json=json.dumps({"text": f"[目标模式] {text}"}, ensure_ascii=False)))
    sess.status = "running"
    await session.commit()
    publish(user.id, "agent", sid, kind="message")
    turn_manager.submit_goal(sid, user.id, body.workflow_id, text)
    return {"ok": True}
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_goal_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/agent.py backend/tests/test_goal_api.py
git commit -m "feat(goal): 目标启动 API + 无质检节点前置校验" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: 工作流版本回滚（gf wf restore + API）

**Files:**
- Modify: `backend/app/routers/runs.py`（`POST /{run_id}/restore`）、`backend/app/cli.py`（`wf restore` 子命令 + `cmd_wf_restore`）
- Test: `backend/tests/test_restore.py`（新建）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_restore.py`：
```python
async def test_restore_run_version_into_workflow(auth_client, session_factory):
    # 造 workflow（graph A）-> 改为 graph B -> 用一次 run 快照了 graph A 的版本
    # POST /api/runs/{run_id}/restore -> 工作流 graph 恢复为 A
    ...
    r = await auth_client.post(f"/api/runs/{run_id}/restore")
    assert r.status_code == 200
    wf = (await auth_client.get(f"/api/workflows/{wf_id}")).json()
    assert wf["graph"] == version_graph_A
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend; python -m pytest tests/test_restore.py -v`
Expected: FAIL（端点不存在）

- [ ] **Step 3: 实现 API + CLI**

`backend/app/routers/runs.py` 增加：
```python
@router.post("/{run_id}/restore")
async def restore_run_version(run_id: int, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    wf.graph_json = ver.graph_json
    await session.commit()
    publish(user.id, "workflow", wf.id)
    return {"ok": True}
```
`backend/app/cli.py`：在 `wf` 子解析器处加 `restore`：
```python
    s = wf.add_parser("restore"); s.add_argument("run_id", type=int); s.set_defaults(func=cmd_wf_restore)
```
并加命令函数（放在 `cmd_wf_rm` 附近）：
```python
def cmd_wf_restore(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/restore")
    print(f"已从运行 #{args.run_id} 的版本恢复工作流图")
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend; python -m pytest tests/test_restore.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/runs.py backend/app/cli.py backend/tests/test_restore.py
git commit -m "feat(goal): 工作流版本回滚 API + gf wf restore" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: 前端目标启动 + 指标展示 + 恢复版本按钮

**Files:**
- Modify: `frontend/src/agent/AgentDrawer.tsx`（目标启动区 + goal_metric 事件 + 指标行）、`frontend/src/pages/RunDetailPage.tsx`（「恢复此版本」按钮）、`frontend/src/api/types.ts`（WorkflowSummary 已有，复用）
- Test: `cd frontend; npm run build`

- [ ] **Step 1: AgentDrawer 加目标启动区与指标**

`frontend/src/agent/AgentDrawer.tsx`：
- 新 state：
```tsx
  const [goalText, setGoalText] = useState('')
  const [goalWf, setGoalWf] = useState<number>()
  const [workflows, setWorkflows] = useState<{ id: number; name: string }[]>([])
  const [metrics, setMetrics] = useState<{ round: number; metric: number | null; run_id: number }[]>([])
```
- 在 `open` 的 effect 里追加拉工作流：
```tsx
    void api.get<{ id: number; name: string }[]>('/api/workflows').then(setWorkflows)
```
- 在 `useEvents` 回调里追加（与 goal_round 同级）：
```tsx
    else if (e.kind === 'goal_metric') setMetrics((m) => [...m, e.data as { round: number; metric: number | null; run_id: number }])
```
并在 `selectSession` 与 `turn_done` 重置处加 `setMetrics([])`。
- 启动函数：
```tsx
  const startGoal = async () => {
    const sid = sessionIdRef.current
    if (!sid || !goalText.trim() || !goalWf) { message.warning('选择工作流并填写目标'); return }
    try {
      setMetrics([])
      await api.post(`/api/agent/sessions/${sid}/goal`, { workflow_id: goalWf, goal_text: goalText })
      setGoalText('')
      await refreshDetail(sid)
    } catch (e) { message.error((e as Error).message) }
  }
```
- 在输入框区域上方渲染目标启动条与指标历史（仅 `detail` 存在且未 running 时显示启动；running 时显示指标）：
```tsx
        {detail && !running && (
          <Space.Compact style={{ width: '100%', marginBottom: 6 }}>
            <Select size="small" style={{ width: 140 }} placeholder="目标工作流"
                    value={goalWf} onChange={setGoalWf}
                    options={workflows.map((w) => ({ value: w.id, label: w.name }))} />
            <Input size="small" placeholder="一句话目标，如：把首轮质检通过率提到 90%"
                   value={goalText} onChange={(e) => setGoalText(e.target.value)} />
            <Button size="small" type="primary" onClick={() => void startGoal()}>目标模式</Button>
          </Space.Compact>
        )}
        {metrics.length > 0 && (
          <div style={{ fontSize: 12, color: '#555', marginBottom: 6 }}>
            {metrics.map((m) => (
              <span key={m.round} style={{ marginRight: 12 }}>
                第{m.round}轮: {m.metric === null ? '—' : `${(m.metric * 100).toFixed(1)}%`}（#{m.run_id}）
              </span>
            ))}
          </div>
        )}
```

- [ ] **Step 2: RunDetailPage 加「恢复此版本」**

`frontend/src/pages/RunDetailPage.tsx` 顶部操作 `Space` 里（`重跑失败行` 按钮后）加：
```tsx
        {!isActive && (
          <Popconfirm title="把当前工作流恢复为此运行的版本？" onConfirm={async () => {
            await api.post(`/api/runs/${id}/restore`); message.success('已恢复工作流版本')
          }}>
            <Button size="small">恢复此版本</Button>
          </Popconfirm>
        )}
```

- [ ] **Step 3: 构建验证**

Run: `cd frontend; npm run build`
Expected: 构建成功

- [ ] **Step 4: 提交**

```bash
git add frontend/src/agent/AgentDrawer.tsx frontend/src/pages/RunDetailPage.tsx
git commit -m "feat(goal): 前端目标启动 + 每轮指标展示 + 恢复版本按钮" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: 全量回归

**Files:** 无（仅跑测试）

- [ ] **Step 1: 后端全量**

Run: `cd backend; python -m pytest -q`
Expected: 全绿（含新增 ~10 个测试文件）。如有红：定位修复，不得跳过。

- [ ] **Step 2: 前端单测 + 构建**

Run: `cd frontend; npx vitest run; npm run build`
Expected: vitest 全过、build 成功

- [ ] **Step 3: 提交（若回归中有修复）**

```bash
git add -A -- backend frontend
git commit -m "test: 多模型质检+Compactor+目标模式 全量回归通过" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 计划自审

**1. Spec 覆盖：**
- F1 多模型 K-of-N → Task 2/3；配置形态/向后兼容 → Task 3（judge_model_ids 回退 model_config_id）、Task 5（校验）、Task 6（CLI）、Task 7（前端）。✓
- 首轮指标 QcMetric + 失败样本 QcFailure → Task 1/3；API → Task 4；运行详情展示+下载 → Task 7。✓
- 级联清理 → Task 4。✓
- F2 model_meta（OpenRouter 窗口+回退）→ Task 8；compactor 三规则+失败跳过 → Task 9；统一接入+compactor 角色默认 → Task 10。✓
- F3 事件驱动等待（RunManager wait + _finish publish）→ Task 11；跑数服务/指标/失败样本/阈值 → Task 12；while 循环+凝练经验+跳出判定+静默兜底 → Task 13；启动 API+无 QC 前置校验 → Task 14；快照回滚（复用 WorkflowVersion）→ Task 15；前端启动+指标+恢复 → Task 16。✓
- 安全：所有模型 user_id 校验（Task 3/5/12）；api_key 不入提示词（compactor 只发历史文本，judge 只发样本）。✓

**2. Placeholder 扫描：** 测试 Task 3/5/6/14/15 的「沿用既有 helper」处用 `...` 占位 —— 这些是**测试脚手架**需实现者读现有同名测试文件复用 helper，不是生产代码占位；每个都给了明确断言与期望。生产代码步骤均为完整可粘贴代码。✓

**3. 类型/签名一致性：**
- `run_qc_judge_row(config, row, mcs, pass_k, user_sem) -> (bool, str, dict, list)` —— Task 2 定义，Task 3 judge_all 按 4 元组解包。✓
- `maybe_compact(history, *, compactor_mc, running_mc, window=None, summarize=None, emit=None)` —— Task 9 定义，Task 10 调用（不传 window，内部查）。✓
- `manager.wait(run_id)` / `manager.submit(...)` —— Task 11 定义，Task 13 调用。✓
- `rs.enqueue_run/first_round_rate/sample_failures/parse_threshold/workflow_has_qc` —— Task 12 定义，Task 13/14 调用。✓
- `gl.decide(...)/build_round_prompt/first_round_prompt` —— Task 13 定义并自用。✓
- 事件 `goal_metric` data 形状 `{round, metric, run_id}` —— Task 13 publish 与 Task 16 消费一致。✓

> 实现者注意：Task 9 中 `pydantic_ai.messages` 的 part 类型以安装版（1.107）实际为准（`ModelRequest` 的 parts 用 `UserPromptPart`；摘要载体若 TextPart 不被接受则改 UserPromptPart）；测试只断言 `.content` 文本，不锁类型。Task 3 改 `run_llm_synth_row` 的 base 过滤为 `not k.startswith("_qc")` 时，确认不影响现有 llm_synth 测试（普通行无 `_qc` 前缀键，行为不变）。
