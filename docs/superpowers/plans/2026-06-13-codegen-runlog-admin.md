# 自动处理增强 + 运行日志/删除 + admin 租户管理 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让自动处理节点的 AI 写代码能力更显眼且生成质量更好；为运行加节点级时间线日志（持久化+展示+下载）与单条级联删除；新增 admin 租户管理（act-as 身份切换 + 用户账号增删）。

**Architecture:** 后端 FastAPI + SQLAlchemy async；新增 `RunLog` 表与 `_log` 落点、`DELETE /runs/{id}` 级联、`admin` 路由（act-as 用签名 cookie，`get_current_user` 返回有效用户）。前端 React 19 + antd：RunDetailPage 日志面板、RunsPage 删除列、AdminPage + 顶栏横幅。act-as 复用全部现有归属端点，不旁路任何校验。

**Tech Stack:** FastAPI, SQLAlchemy 2 async, pydantic-settings, pytest + httpx ASGITransport；React 19, antd 6, React Flow, vitest。

**对应 spec：** `docs/superpowers/specs/2026-06-13-codegen-runlog-admin-design.md`

**测试命令：** 后端 `cd backend && uv run pytest -q`；前端 `cd frontend && npx vitest run` + `npm run build`。

**KISS 红线：** api_key 绝不出现在响应/日志/Agent 提示；每用户资源隔离是硬验收；不预防未发生的 bug。

---

## Task 1: 自动处理 codegen INSTRUCTIONS 增强（分组去重指引）+ 子进程能力验证

**Files:**
- Modify: `backend/app/agent/codegen.py:15-20`（`INSTRUCTIONS`）
- Test: `backend/tests/test_pycode.py`（加 pandas 分组去重子进程实测）
- Test: `backend/tests/test_agent_codegen.py`（加 INSTRUCTIONS 断言）

- [ ] **Step 1: 写失败测试（INSTRUCTIONS 断言 + 子进程分组去重）**

在 `backend/tests/test_agent_codegen.py` 末尾追加：

```python
def test_instructions_guide_grouped_dedup():
    from app.agent.codegen import INSTRUCTIONS
    assert "pandas" in INSTRUCTIONS
    assert "分组" in INSTRUCTIONS  # 引导「先按 session 分组再去重」一类复杂处理
```

在 `backend/tests/test_pycode.py` 末尾追加（确定性验证子进程支持 pandas 分组去重，A1 依赖此运行时能力）：

```python
async def test_pandas_grouped_dedup_runs_in_subprocess():
    rows = [
        {"session": "s1", "q": "a"}, {"session": "s1", "q": "a"},
        {"session": "s1", "q": "b"}, {"session": "s2", "q": "a"},
    ]
    code = (
        "import pandas as pd\n"
        "def process(rows):\n"
        "    df = pd.DataFrame(rows)\n"
        "    df = df.drop_duplicates(subset=['session', 'q'])\n"
        "    return df.to_dict('records')\n"
    )
    out = await run_process_code(code, rows)
    assert out == [
        {"session": "s1", "q": "a"}, {"session": "s1", "q": "b"}, {"session": "s2", "q": "a"},
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_agent_codegen.py::test_instructions_guide_grouped_dedup tests/test_pycode.py::test_pandas_grouped_dedup_runs_in_subprocess -v`
Expected: `test_instructions_guide_grouped_dedup` FAIL（当前 INSTRUCTIONS 无「分组」）；分组去重子进程测试预期 PASS（pandas 已装，验证运行时能力）。

- [ ] **Step 3: 增强 INSTRUCTIONS**

把 `backend/app/agent/codegen.py` 的 `INSTRUCTIONS` 整体替换为：

```python
INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
硬性要求：
- 只输出 Python 源码，不要任何解释或 markdown 围栏。
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas（可 import pandas as pd）；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。

常见模式（按需选用、灵活组合，最后都 return 行字典列表，如 df.to_dict('records')）：
- 全局去重：df.drop_duplicates(subset=[列...])。
- 分组内去重（如先按 session 分组再组内去重）：df.drop_duplicates(subset=['session', 列...])；
  需要更复杂的组内逻辑时用 df.groupby('session', group_keys=False).apply(fn)。
- 过滤/改列：用 pandas 布尔索引或列表推导。"""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_agent_codegen.py tests/test_pycode.py -q`
Expected: PASS（全部）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/codegen.py backend/tests/test_agent_codegen.py backend/tests/test_pycode.py
git commit -m "feat: codegen 增强分组去重指引 + 子进程 pandas 能力验证"
```

---

## Task 2: 前端 AutoProcessForm 加「用 AI 写处理代码」主按钮

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`AutoProcessForm`，约 307-331 行）

无组件测试基础设施，本任务以 `npm run build` 验证（沿用既有前端任务约定）。

- [ ] **Step 1: 改 AutoProcessForm 的底部按钮区**

在 `AutoProcessForm` 里，把现有的单个

```tsx
      <Button block onClick={() => setOps([...ops, { ...OP_DEFAULTS.dedup }])}>+ 添加操作</Button>
```

替换为：

```tsx
      <Space direction="vertical" style={{ width: '100%' }}>
        <Button block onClick={() => setOps([...ops, { ...OP_DEFAULTS.dedup }])}>+ 添加操作</Button>
        <Button block type="dashed" onClick={() => setOps([...ops, { ...OP_DEFAULTS.agent }])}>
          ✨ 用 AI 写处理代码
        </Button>
        <div style={{ color: '#999', fontSize: 12 }}>复杂处理（如按 session 分组去重）建议用 AI 写代码。</div>
      </Space>
```

（`Space`、`Button` 已在文件顶部从 antd 导入，无需新增 import。）

- [ ] **Step 2: 构建验证**

Run: `cd frontend && npm run build`
Expected: 构建成功（`✓ built`）。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat: 自动处理节点加「用 AI 写处理代码」主按钮"
```

---

## Task 3: RunLog 数据模型

**Files:**
- Modify: `backend/app/models.py`（在 `RunRow` 类之后新增 `RunLog`）
- Test: `backend/tests/test_run_logs.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_run_logs.py`：

```python
from sqlalchemy import select


async def test_runlog_insert_and_defaults(client, session_factory):
    from app.models import RunLog
    async with session_factory() as s:
        s.add(RunLog(run_id=1, node_id="n1", message="hi"))
        await s.commit()
    async with session_factory() as s:
        rows = (await s.execute(select(RunLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].message == "hi"
    assert rows[0].level == "info"
    assert rows[0].node_id == "n1"
    assert rows[0].created_at is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_run_logs.py -v`
Expected: FAIL（`ImportError: cannot import name 'RunLog'`）。

- [ ] **Step 3: 新增 RunLog 模型**

在 `backend/app/models.py` 的 `RunRow` 类定义之后插入：

```python
class RunLog(Base):
    __tablename__ = "run_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")  # "" 表示运行级事件
    level: Mapped[str] = mapped_column(default="info")  # info / error
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

（`Mapped`/`mapped_column`/`ForeignKey`/`Text`/`DateTime`/`datetime`/`now` 均已在文件顶部可用。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_run_logs.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/models.py backend/tests/test_run_logs.py
git commit -m "feat: 新增 RunLog 模型（运行级时间线日志）"
```

---

## Task 4: runner 落运行日志（_log + _node_counts + 落点）

**Files:**
- Modify: `backend/app/engine/runner.py:10`（import 加 `RunLog`）
- Modify: `backend/app/engine/runner.py`（`execute_run` except、`_execute` 循环、`_finish`，新增 `_log`/`_node_counts`）
- Test: `backend/tests/test_run_logs.py`（加运行落日志测试）

- [ ] **Step 1: 写失败测试**

直接加到 `backend/tests/test_runs_api.py` 末尾（该文件已有 `patch_chat`/`setup_workflow`/`wait_run`，避免跨测试模块 import）：

```python
async def test_run_emits_node_and_run_logs(auth_client, monkeypatch, session_factory):
    from sqlalchemy import select
    from app.models import RunLog
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    async with session_factory() as s:
        logs = (await s.execute(
            select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id))).scalars().all()
    msgs = [l.message for l in logs]
    assert any("运行开始" in m for m in msgs)
    assert any("节点 gen 开始" in m for m in msgs)
    assert any("节点 gen 完成" in m for m in msgs)
    assert any("运行结束" in m for m in msgs)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_runs_api.py::test_run_emits_node_and_run_logs -v`
Expected: FAIL（无「运行开始」等日志）。

- [ ] **Step 3: import 加 RunLog**

把 `backend/app/engine/runner.py` 第 10 行：

```python
from app.models import DatasetRow, ModelConfig, Run, RunNodeState, RunRow, WorkflowVersion
```

改为：

```python
from app.models import DatasetRow, ModelConfig, Run, RunLog, RunNodeState, RunRow, WorkflowVersion
```

- [ ] **Step 4: 新增 _log 与 _node_counts 助手**

在 `runner.py` 的 `_cancellable` 函数之后（`execute_run` 之前）插入：

```python
async def _log(session_factory, run_id, node_id, message, level="info"):
    async with session_factory() as s:
        s.add(RunLog(run_id=run_id, node_id=node_id, message=message, level=level))
        await s.commit()


async def _node_counts(session_factory, run_id, node_id) -> tuple[int, int]:
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == node_id))).scalar_one_or_none()
    return (ns.done, ns.failed) if ns else (0, 0)
```

- [ ] **Step 5: execute_run 失败落日志**

把 `execute_run` 的 except 块（约 42-48 行）：

```python
    except Exception as e:
        async with session_factory() as s:
            run = await s.get(Run, run_id)
            run.status = "failed"
            run.error = str(e)
            run.finished_at = _now()
            await s.commit()
```

改为（末尾加一行 `_log`）：

```python
    except Exception as e:
        async with session_factory() as s:
            run = await s.get(Run, run_id)
            run.status = "failed"
            run.error = str(e)
            run.finished_at = _now()
            await s.commit()
        await _log(session_factory, run_id, "", f"运行失败：{e}", "error")
```

- [ ] **Step 6: _execute 循环落「运行开始 / 节点开始 / 节点完成」**

把 `_execute` 的 `validate_graph(graph)` 之后到 `await _finish(..., "completed")` 之间（约 60-76 行）：

```python
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event)
        elif node.type == "qc":
            await _run_qc_node(session_factory, run_id, user_id, graph, node, inputs,
                               user_sem, cancel_event)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
    await _finish(session_factory, run_id, "completed")
```

替换为：

```python
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)
    await _log(session_factory, run_id, "", "运行开始")

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
        await _log(session_factory, run_id, node.id, f"▶ 节点 {node.id} 开始")
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event)
        elif node.type == "qc":
            await _run_qc_node(session_factory, run_id, user_id, graph, node, inputs,
                               user_sem, cancel_event)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        done, failed = await _node_counts(session_factory, run_id, node.id)
        await _log(session_factory, run_id, node.id,
                   f"✓ 节点 {node.id} 完成（done={done} failed={failed}）",
                   "error" if failed else "info")
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled")
    await _finish(session_factory, run_id, "completed")
```

- [ ] **Step 7: _finish 落「运行结束」**

把 `_finish` 函数体末尾的 `await s.commit()` 之后补一行（注意 `_log` 在 `async with` 块外、`sums` 仍在作用域）：

```python
async def _finish(session_factory, run_id, status):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
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
```

- [ ] **Step 8: 运行测试确认通过 + 回归 runner**

Run: `cd backend && uv run pytest tests/test_run_logs.py tests/test_runner.py tests/test_runs_api.py -q`
Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_runs_api.py
git commit -m "feat: runner 落节点级时间线日志（运行/节点 开始结束）"
```

---

## Task 5: GET /api/runs/{id}/logs 端点

**Files:**
- Modify: `backend/app/routers/runs.py`（import 加 `RunLog`，新增端点）
- Test: `backend/tests/test_runs_api.py`（加端点测试）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_runs_api.py` 末尾追加：

```python
async def test_run_logs_endpoint(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    logs = (await auth_client.get(f"/api/runs/{run_id}/logs")).json()
    assert any("运行开始" in l["message"] for l in logs)
    assert all({"created_at", "node_id", "level", "message"} <= set(l) for l in logs)


async def test_run_logs_foreign_rejected(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await wait_run(auth_client, run_id)
    await auth_client.post("/api/auth/login", json={"username": "intruder"})
    r = await auth_client.get(f"/api/runs/{run_id}/logs")
    assert r.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_runs_api.py::test_run_logs_endpoint -v`
Expected: FAIL（404 路由不存在 / 响应非列表）。

- [ ] **Step 3: import 加 RunLog**

把 `backend/app/routers/runs.py` 的模型 import（约 18-19 行）：

```python
from app.models import (Dataset, ModelConfig, Run, RunNodeState, RunRow, User,
                        Workflow, WorkflowVersion)
```

改为：

```python
from app.models import (Dataset, ModelConfig, Run, RunLog, RunNodeState, RunRow, User,
                        Workflow, WorkflowVersion)
```

- [ ] **Step 4: 新增 logs 端点**

在 `backend/app/routers/runs.py` 的 `run_detail` 端点之后插入：

```python
@router.get("/{run_id}/logs")
async def run_logs(run_id: int, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    logs = (await session.execute(
        select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id))).scalars().all()
    return [{"created_at": l.created_at.isoformat(), "node_id": l.node_id,
             "level": l.level, "message": l.message} for l in logs]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_runs_api.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/routers/runs.py backend/tests/test_runs_api.py
git commit -m "feat: GET /runs/{id}/logs 返回运行时间线日志"
```

---

## Task 6: DELETE /api/runs/{id} 级联删除

**Files:**
- Modify: `backend/app/routers/runs.py`（新增 `delete_run` 端点）
- Test: `backend/tests/test_runs_api.py`（加删除测试）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_runs_api.py` 末尾追加：

```python
async def test_delete_run_cascades(auth_client, monkeypatch, session_factory):
    from sqlalchemy import func, select
    from app.models import Run, RunLog, RunNodeState, RunRow, WorkflowVersion
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    async with session_factory() as s:
        ver_id = (await s.execute(
            select(Run.workflow_version_id).where(Run.id == run_id))).scalar()
    assert (await auth_client.delete(f"/api/runs/{run_id}")).status_code == 200
    assert (await auth_client.get(f"/api/runs/{run_id}")).status_code == 404
    async with session_factory() as s:
        for model in (RunRow, RunNodeState, RunLog):
            cnt = (await s.execute(
                select(func.count()).select_from(model).where(model.run_id == run_id))).scalar()
            assert cnt == 0
        ver = (await s.execute(
            select(func.count()).select_from(WorkflowVersion)
            .where(WorkflowVersion.id == ver_id))).scalar()
        assert ver == 0


async def test_delete_running_rejected(auth_client, monkeypatch):
    async def slow(mc, system, user, params=None, retries=3):
        await asyncio.sleep(0.3)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(llm, "chat", slow)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    assert (await auth_client.delete(f"/api/runs/{run_id}")).status_code == 409
    await auth_client.post(f"/api/runs/{run_id}/cancel")
    await wait_run(auth_client, run_id)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_runs_api.py::test_delete_run_cascades tests/test_runs_api.py::test_delete_running_rejected -v`
Expected: FAIL（405/404，DELETE 路由不存在）。

- [ ] **Step 3: 新增 delete_run 端点**

在 `backend/app/routers/runs.py` 的 `run_detail` 端点之后（或 logs 端点之后）插入：

```python
@router.delete("/{run_id}")
async def delete_run(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status in ("queued", "running"):
        raise HTTPException(status_code=409, detail="运行中，请先取消再删除")
    ver_id = run.workflow_version_id
    await session.execute(sa_delete(RunRow).where(RunRow.run_id == run_id))
    await session.execute(sa_delete(RunNodeState).where(RunNodeState.run_id == run_id))
    await session.execute(sa_delete(RunLog).where(RunLog.run_id == run_id))
    await session.execute(sa_delete(Run).where(Run.id == run_id))
    await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.id == ver_id))
    await session.commit()
    for p in (settings.data_dir / "exports").glob(f"run{run_id}_*"):
        p.unlink(missing_ok=True)
    publish(user.id, "run", run_id)
    return {"ok": True}
```

（`sa_delete`、`settings`、`publish`、`HTTPException`、`RunLog` 均已在 `runs.py` 顶部 import。）

- [ ] **Step 4: 运行测试确认通过 + 回归**

Run: `cd backend && uv run pytest tests/test_runs_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/runs.py backend/tests/test_runs_api.py
git commit -m "feat: DELETE /runs/{id} 级联清理行/状态/日志/版本/导出文件"
```

---

## Task 7: 前端运行日志面板 + 下载 + 运行删除列

**Files:**
- Modify: `frontend/src/api/types.ts`（加 `RunLogEntry`）
- Create: `frontend/src/pages/runLog.ts`（日志转文本，可单测）
- Create: `frontend/src/pages/runLog.test.ts`
- Modify: `frontend/src/pages/RunDetailPage.tsx`（日志面板 + 下载）
- Modify: `frontend/src/pages/RunsPage.tsx`（删除列）

- [ ] **Step 1: 写失败单测（日志转文本）**

新建 `frontend/src/pages/runLog.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import { formatRunLog } from './runLog'

describe('formatRunLog', () => {
  it('每条渲染成带时间戳与等级的一行', () => {
    const text = formatRunLog([
      { created_at: 't1', node_id: '', level: 'info', message: '运行开始' },
      { created_at: 't2', node_id: 'gen', level: 'error', message: '节点 gen 失败' },
    ])
    expect(text).toBe('[t1] INFO 运行开始\n[t2] ERROR 节点 gen 失败')
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/pages/runLog.test.ts`
Expected: FAIL（`runLog` 模块不存在）。

- [ ] **Step 3: 加类型与 formatRunLog**

在 `frontend/src/api/types.ts` 末尾追加：

```ts
export interface RunLogEntry { created_at: string; node_id: string; level: string; message: string }
```

新建 `frontend/src/pages/runLog.ts`：

```ts
import type { RunLogEntry } from '../api/types'

export function formatRunLog(entries: RunLogEntry[]): string {
  return entries.map((e) => `[${e.created_at}] ${e.level.toUpperCase()} ${e.message}`).join('\n')
}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/pages/runLog.test.ts`
Expected: PASS。

- [ ] **Step 5: RunDetailPage 加日志面板 + 下载**

在 `frontend/src/pages/RunDetailPage.tsx`：

5a. import 行加 `Card` 已在、需要 `formatRunLog` 与类型。把第 5 行：

```tsx
import type { RowsPage, RunDetail } from '../api/types'
```

改为：

```tsx
import type { RowsPage, RunDetail, RunLogEntry } from '../api/types'
import { formatRunLog } from './runLog'
```

5b. 在组件内 `const [format, setFormat] = useState('jsonl')` 之后加日志状态与拉取：

```tsx
  const [logs, setLogs] = useState<RunLogEntry[]>([])
  const refreshLogs = useCallback(
    () => api.get<RunLogEntry[]>(`/api/runs/${id}/logs`).then(setLogs), [id])
  useEffect(() => { void refreshLogs() }, [refreshLogs])
  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => void refreshLogs(), 2000)
    return () => clearInterval(t)
  }, [run?.status, refreshLogs])
  const downloadLog = () => {
    const blob = new Blob([formatRunLog(logs)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `run${id}.log`
    a.click()
    URL.revokeObjectURL(url)
  }
```

5c. 在节点状态 `</Space>`（`orderedStates.map` 所在的 Space 闭合）之后、`{!isActive && (` 之前插入日志卡片：

```tsx
      <Card size="small" title="运行日志" style={{ marginBottom: 16 }}
            extra={<Button size="small" onClick={downloadLog} disabled={!logs.length}>下载日志</Button>}>
        <div style={{ maxHeight: 220, overflow: 'auto', fontFamily: 'monospace', fontSize: 12 }}>
          {logs.map((l, i) => (
            <div key={i} style={{ color: l.level === 'error' ? '#ff4d4f' : '#555' }}>
              [{l.created_at}] {l.message}
            </div>
          ))}
          {!logs.length && <span style={{ color: '#999' }}>暂无日志</span>}
        </div>
      </Card>
```

（`Card`、`Button` 已在该文件顶部 antd import 中。）

- [ ] **Step 6: RunsPage 加删除列**

在 `frontend/src/pages/RunsPage.tsx`：

6a. 第 2 行：

```tsx
import { Table, Tag } from 'antd'
```

改为：

```tsx
import { Button, Popconfirm, Table, Tag, message } from 'antd'
```

6b. 在 columns 数组末尾（`{ title: '结束时间', dataIndex: 'finished_at' },` 之后）加一列：

```tsx
        {
          title: '操作', key: 'act',
          render: (_: unknown, r: Run) => (
            <Popconfirm title="删除该运行及其全部数据？"
                        onConfirm={async () => { await api.del(`/api/runs/${r.id}`); message.success('已删除'); await reload() }}>
              <Button danger size="small" disabled={['queued', 'running'].includes(r.status)}>删除</Button>
            </Popconfirm>
          ),
        },
```

- [ ] **Step 7: 全前端测试 + 构建**

Run: `cd frontend && npx vitest run && npm run build`
Expected: vitest 全 PASS；构建成功。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/pages/runLog.ts frontend/src/pages/runLog.test.ts frontend/src/pages/RunDetailPage.tsx frontend/src/pages/RunsPage.tsx
git commit -m "feat: 运行详情日志面板+下载，运行列表删除列"
```

---

## Task 8: User.is_admin + Settings.admin_users + 登录刷新

**Files:**
- Modify: `backend/app/models.py`（`User` 加 `is_admin`）
- Modify: `backend/app/config.py`（加 `admin_users` + `admin_user_set`）
- Modify: `backend/app/auth.py`（`DevAuthProvider.login` 刷新 is_admin）
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_auth.py` 末尾追加：

```python
async def test_admin_flag_set_on_login(client, monkeypatch, session_factory):
    from sqlalchemy import select
    from app.config import settings
    from app.models import User
    monkeypatch.setattr(settings, "admin_users", "boss,root")
    await client.post("/api/auth/login", json={"username": "boss"})
    await client.post("/api/auth/login", json={"username": "pleb"})
    async with session_factory() as s:
        boss = (await s.execute(select(User).where(User.username == "boss"))).scalar_one()
        pleb = (await s.execute(select(User).where(User.username == "pleb"))).scalar_one()
    assert boss.is_admin is True
    assert pleb.is_admin is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_auth.py::test_admin_flag_set_on_login -v`
Expected: FAIL（`User` 无 `is_admin`）。

- [ ] **Step 3: User 加 is_admin**

在 `backend/app/models.py` 的 `User` 类，`max_llm_concurrency` 行之后加：

```python
    is_admin: Mapped[bool] = mapped_column(default=False)
```

- [ ] **Step 4: Settings 加 admin_users**

把 `backend/app/config.py` 的 `Settings` 改为：

```python
class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"
    agent_goal_max_rounds: int = 20
    admin_users: str = ""  # 逗号分隔的管理员用户名白名单

    @property
    def admin_user_set(self) -> set[str]:
        return {u.strip() for u in self.admin_users.split(",") if u.strip()}

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir.as_posix()}/graphflow.db"
```

- [ ] **Step 5: 登录刷新 is_admin**

把 `backend/app/auth.py` 的 `DevAuthProvider.login` 改为：

```python
    async def login(self, session: AsyncSession, username: str) -> User:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, display_name=username, auth_provider="dev")
            session.add(user)
        user.is_admin = username in settings.admin_user_set
        await session.commit()
        return user
```

（`settings` 已在 `auth.py` 顶部 import。）

- [ ] **Step 6: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_auth.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/models.py backend/app/config.py backend/app/auth.py backend/tests/test_auth.py
git commit -m "feat: User.is_admin + GRAPHFLOW_ADMIN_USERS 白名单，登录刷新"
```

---

## Task 9: auth 有效用户切换（get_real_user / get_current_user / require_admin）

**Files:**
- Modify: `backend/app/auth.py`（act-as cookie 助手、`get_real_user`、改 `get_current_user`、`require_admin`）
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: 写失败测试（非管理员伪造 act-as 无效）**

在 `backend/tests/test_auth.py` 末尾追加：

```python
async def test_act_as_ignored_for_non_admin(client, monkeypatch):
    from app.auth import make_act_as_cookie
    victim = (await client.post("/api/auth/login", json={"username": "victim"})).json()
    await client.post("/api/auth/login", json={"username": "pleb"})  # cookie 现为 pleb
    client.cookies.set("gf_act_as", make_act_as_cookie(victim["id"]))
    me = (await client.get("/api/me")).json()
    assert me["username"] == "pleb"  # 非管理员即便带签名 act-as 也不切换


async def test_act_as_cookie_roundtrip():
    from app.auth import make_act_as_cookie, parse_session_cookie
    assert parse_session_cookie(make_act_as_cookie(42)) == 42
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_auth.py::test_act_as_cookie_roundtrip -v`
Expected: FAIL（`make_act_as_cookie` 不存在）。

- [ ] **Step 3: auth.py 加 act-as 与有效用户逻辑**

在 `backend/app/auth.py`：

3a. 在 `COOKIE_MAX_AGE = 7 * 24 * 3600` 之后加：

```python
ACT_AS_COOKIE = "gf_act_as"
```

3b. 在 `parse_session_cookie` 之后加：

```python
def make_act_as_cookie(user_id: int) -> str:
    return TimestampSigner(settings.secret_key).sign(str(user_id)).decode()
```

3c. 把现有 `get_current_user` 整段替换为下面三个定义（新增 `get_real_user`、改 `get_current_user`、新增 `require_admin`）：

```python
async def get_real_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    gf_act_as: str | None = Cookie(default=None, alias=ACT_AS_COOKIE),
) -> User:
    """返回有效用户：仅当真实用户是管理员且 act-as cookie 有效时切换为目标用户。"""
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if user.is_admin and gf_act_as:
        target_id = parse_session_cookie(gf_act_as)
        target = await session.get(User, target_id) if target_id is not None else None
        if target is not None:
            return target
    return user


async def require_admin(user: User = Depends(get_real_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
```

- [ ] **Step 4: 运行确认通过 + 回归 auth/runs/datasets**

Run: `cd backend && uv run pytest tests/test_auth.py tests/test_runs_api.py tests/test_datasets.py -q`
Expected: PASS（现有归属端点用有效用户，非管理员行为不变）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/auth.py backend/tests/test_auth.py
git commit -m "feat: auth 有效用户切换（act-as）+ require_admin，守住非管理员隔离"
```

---

## Task 10: admin 路由（act-as + 用户 CRUD + 级联删用户）

**Files:**
- Create: `backend/app/routers/admin.py`
- Modify: `backend/app/main.py`（注册 admin 路由）
- Test: `backend/tests/test_admin_api.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_admin_api.py`：

```python
from sqlalchemy import func, select

from app.config import settings


async def _login_admin(client, monkeypatch, name="boss"):
    monkeypatch.setattr(settings, "admin_users", name)
    return (await client.post("/api/auth/login", json={"username": name})).json()


async def test_non_admin_forbidden(client):
    await client.post("/api/auth/login", json={"username": "pleb"})
    assert (await client.get("/api/admin/users")).status_code == 403


async def test_list_and_create_users(client, monkeypatch):
    await _login_admin(client, monkeypatch)
    r = await client.post("/api/admin/users", json={"username": "alice"})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    assert (await client.post("/api/admin/users", json={"username": "alice"})).status_code == 422
    users = (await client.get("/api/admin/users")).json()
    assert {u["username"] for u in users} >= {"boss", "alice"}


async def test_act_as_lets_admin_operate_as_user(client, monkeypatch, session_factory):
    from app.models import Dataset
    await _login_admin(client, monkeypatch)
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    assert (await client.get("/api/me")).json()["username"] == "alice"
    files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
    await client.post("/api/datasets/upload", files=files)
    await client.post("/api/admin/act-as", json={"user_id": None})
    assert (await client.get("/api/me")).json()["username"] == "boss"
    async with session_factory() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(Dataset).where(Dataset.user_id == alice["id"]))).scalar()
    assert cnt == 1  # 数据集归属被切换的 alice


async def test_delete_user_cascade(client, monkeypatch, session_factory):
    from app.models import Dataset, User
    await _login_admin(client, monkeypatch)
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
    await client.post("/api/datasets/upload", files=files)
    await client.post("/api/admin/act-as", json={"user_id": None})
    assert (await client.delete(f"/api/admin/users/{alice['id']}")).status_code == 200
    async with session_factory() as s:
        assert (await s.execute(
            select(func.count()).select_from(User).where(User.id == alice["id"]))).scalar() == 0
        assert (await s.execute(
            select(func.count()).select_from(Dataset).where(Dataset.user_id == alice["id"]))).scalar() == 0


async def test_cannot_delete_self(client, monkeypatch):
    admin = await _login_admin(client, monkeypatch)
    assert (await client.delete(f"/api/admin/users/{admin['id']}")).status_code == 409
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_admin_api.py -v`
Expected: FAIL（404，/api/admin 未注册）。

- [ ] **Step 3: 新建 admin 路由**

新建 `backend/app/routers/admin.py`：

```python
import shutil

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.turns import _safe
from app.auth import ACT_AS_COOKIE, COOKIE_MAX_AGE, make_act_as_cookie, require_admin
from app.config import settings
from app.db import get_session
from app.models import (AgentMessage, AgentSession, Dataset, DatasetRow, ModelConfig,
                        Run, RunLog, RunNodeState, RunRow, User, Workflow, WorkflowVersion)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_row(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "is_admin": u.is_admin, "created_at": u.created_at.isoformat()}


class ActAsIn(BaseModel):
    user_id: int | None


@router.post("/act-as")
async def act_as(body: ActAsIn, response: Response, admin: User = Depends(require_admin),
                 session: AsyncSession = Depends(get_session)):
    if body.user_id is None:
        response.delete_cookie(ACT_AS_COOKIE)
        return _user_row(admin)
    target = await session.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    response.set_cookie(ACT_AS_COOKIE, make_act_as_cookie(target.id),
                        httponly=True, max_age=COOKIE_MAX_AGE)
    return _user_row(target)


@router.get("/users")
async def list_users(admin: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    users = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return [_user_row(u) for u in users]


class UserCreate(BaseModel):
    username: str
    display_name: str = ""


@router.post("/users")
async def create_user(body: UserCreate, admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    if (await session.execute(select(User).where(User.username == username))).scalar_one_or_none():
        raise HTTPException(status_code=422, detail="用户名已存在")
    user = User(username=username, display_name=body.display_name or username,
                is_admin=username in settings.admin_user_set)
    session.add(user)
    await session.commit()
    return _user_row(user)


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="不能删除自己")
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = target.username
    ds_ids = (await session.execute(
        select(Dataset.id).where(Dataset.user_id == user_id))).scalars().all()
    run_ids = (await session.execute(
        select(Run.id).where(Run.user_id == user_id))).scalars().all()
    wf_ids = (await session.execute(
        select(Workflow.id).where(Workflow.user_id == user_id))).scalars().all()
    sess_ids = (await session.execute(
        select(AgentSession.id).where(AgentSession.user_id == user_id))).scalars().all()
    if ds_ids:
        await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id.in_(ds_ids)))
    await session.execute(sa_delete(Dataset).where(Dataset.user_id == user_id))
    await session.execute(sa_delete(ModelConfig).where(ModelConfig.user_id == user_id))
    if run_ids:
        await session.execute(sa_delete(RunRow).where(RunRow.run_id.in_(run_ids)))
        await session.execute(sa_delete(RunNodeState).where(RunNodeState.run_id.in_(run_ids)))
        await session.execute(sa_delete(RunLog).where(RunLog.run_id.in_(run_ids)))
    await session.execute(sa_delete(Run).where(Run.user_id == user_id))
    if wf_ids:
        await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(wf_ids)))
    await session.execute(sa_delete(Workflow).where(Workflow.user_id == user_id))
    if sess_ids:
        await session.execute(sa_delete(AgentMessage).where(AgentMessage.session_id.in_(sess_ids)))
    await session.execute(sa_delete(AgentSession).where(AgentSession.user_id == user_id))
    await session.execute(sa_delete(User).where(User.id == user_id))
    await session.commit()
    # 该用户上传的数据集文件都在 uploads/<user_id>/，整目录删除即可（运行结果数据集 file_path 为空）
    shutil.rmtree(settings.data_dir / "uploads" / str(user_id), ignore_errors=True)
    shutil.rmtree(settings.data_dir / "agent" / _safe(username), ignore_errors=True)
    for rid in run_ids:
        for p in (settings.data_dir / "exports").glob(f"run{rid}_*"):
            p.unlink(missing_ok=True)
    return {"ok": True}
```

- [ ] **Step 4: 注册 admin 路由**

在 `backend/app/main.py`：第 11 行 import 加 `admin`：

```python
from app.routers import admin, agent, auth, datasets, events, model_configs, runs, workflows
```

在 `create_app` 的 `app.include_router(auth.router)` 之后加：

```python
    app.include_router(admin.router)
```

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_admin_api.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/routers/admin.py backend/app/main.py backend/tests/test_admin_api.py
git commit -m "feat: admin 路由 act-as + 用户增删（级联清理全部资源）"
```

---

## Task 11: /api/me 暴露 is_admin / acting_as / real_username

**Files:**
- Modify: `backend/app/routers/auth.py`（`me` 端点）
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_auth.py` 末尾追加：

```python
async def test_me_exposes_admin_fields(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_users", "boss")
    await client.post("/api/auth/login", json={"username": "boss"})
    me = (await client.get("/api/me")).json()
    assert me["is_admin"] is True
    assert me["acting_as"] is None
    assert me["real_username"] == "boss"


async def test_me_acting_as(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_users", "boss")
    await client.post("/api/auth/login", json={"username": "boss"})
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    me = (await client.get("/api/me")).json()
    assert me["username"] == "alice"
    assert me["acting_as"] == "alice"
    assert me["real_username"] == "boss"
    assert me["is_admin"] is True  # 仍反映真实管理员
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_auth.py::test_me_exposes_admin_fields -v`
Expected: FAIL（响应无 `is_admin` 字段）。

- [ ] **Step 3: 改 me 端点**

在 `backend/app/routers/auth.py`：第 5 行 import 加 `get_real_user`（在原有名字基础上补一个即可）：

```python
from app.auth import (COOKIE_MAX_AGE, COOKIE_NAME, auth_provider,
                      get_current_user, get_real_user, make_session_cookie)
```

把 `me` 端点替换为：

```python
@router.get("/me")
async def me(real: User = Depends(get_real_user), effective: User = Depends(get_current_user)):
    return {**_user_out(effective), "is_admin": real.is_admin,
            "real_username": real.username,
            "acting_as": effective.username if effective.id != real.id else None}
```

- [ ] **Step 4: 运行确认通过 + 回归 auth/admin**

Run: `cd backend && uv run pytest tests/test_auth.py tests/test_admin_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/auth.py backend/tests/test_auth.py
git commit -m "feat: /api/me 暴露 is_admin/acting_as/real_username"
```

---

## Task 12: 前端 admin 页面 + 顶栏横幅 + act-as

**Files:**
- Modify: `frontend/src/api/types.ts`（`UserInfo` 扩展 + `AdminUser`）
- Modify: `frontend/src/stores/auth.ts`（加 `actAs`）
- Create: `frontend/src/pages/AdminPage.tsx`
- Modify: `frontend/src/App.tsx`（路由 + 菜单 + 横幅）

无组件测试，本任务以 vitest（既有）+ build 验证。

- [ ] **Step 1: 扩展类型**

在 `frontend/src/api/types.ts`：把第 1 行：

```ts
export interface UserInfo { id: number; username: string; display_name: string }
```

改为：

```ts
export interface UserInfo {
  id: number; username: string; display_name: string
  is_admin: boolean; acting_as: string | null; real_username: string
}
export interface AdminUser {
  id: number; username: string; display_name: string; is_admin: boolean; created_at: string
}
```

- [ ] **Step 2: auth store 加 actAs**

把 `frontend/src/stores/auth.ts` 整体替换为：

```ts
import { create } from 'zustand'
import { api } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  ready: boolean
  init: () => Promise<void>
  login: (username: string) => Promise<void>
  logout: () => Promise<void>
  actAs: (userId: number | null) => Promise<void>
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  ready: false,
  init: async () => {
    try {
      set({ user: await api.get<UserInfo>('/api/me'), ready: true })
    } catch {
      set({ user: null, ready: true })
    }
  },
  login: async (username) => {
    await api.post<UserInfo>('/api/auth/login', { username })
    await get().init()
  },
  logout: async () => {
    await api.post('/api/auth/logout')
    set({ user: null })
  },
  actAs: async (userId) => {
    await api.post('/api/admin/act-as', { user_id: userId })
    await get().init()
  },
}))
```

（注意 `login` 改为登录后 `init()` 拉取完整 `/api/me`，确保 `is_admin` 等字段就位。）

- [ ] **Step 3: 新建 AdminPage**

新建 `frontend/src/pages/AdminPage.tsx`：

```tsx
import { useCallback, useEffect, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Space, Table, Tag, message } from 'antd'
import { api } from '../api/client'
import type { AdminUser } from '../api/types'
import { useAuth } from '../stores/auth'

export default function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const actAs = useAuth((s) => s.actAs)
  const reload = useCallback(() => api.get<AdminUser[]>('/api/admin/users').then(setUsers), [])
  useEffect(() => { void reload() }, [reload])

  const create = async () => {
    try {
      await api.post('/api/admin/users', { username: newName.trim() })
      setCreating(false)
      setNewName('')
      message.success('已创建')
      await reload()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}>租户管理</h3>
        <Button type="primary" size="small" onClick={() => setCreating(true)}>新建用户</Button>
      </Space>
      <Table
        rowKey="id"
        dataSource={users}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 70 },
          { title: '用户名', dataIndex: 'username' },
          { title: '显示名', dataIndex: 'display_name' },
          { title: '管理员', dataIndex: 'is_admin', render: (v: boolean) => (v ? <Tag color="gold">admin</Tag> : null) },
          { title: '创建时间', dataIndex: 'created_at' },
          {
            title: '操作', key: 'act',
            render: (_: unknown, u: AdminUser) => (
              <Space>
                <Button size="small" onClick={async () => { await actAs(u.id); message.success(`已切换为 ${u.username}`) }}>
                  以此身份操作
                </Button>
                <Popconfirm title="删除该用户及其全部资源？" onConfirm={async () => {
                  try { await api.del(`/api/admin/users/${u.id}`); message.success('已删除'); await reload() }
                  catch (e) { message.error((e as Error).message) }
                }}>
                  <Button danger size="small">删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal open={creating} title="新建用户" onCancel={() => setCreating(false)} onOk={() => void create()}>
        <Input placeholder="用户名" value={newName} onChange={(e) => setNewName(e.target.value)} />
      </Modal>
    </>
  )
}
```

- [ ] **Step 4: App.tsx 加路由/菜单/横幅**

在 `frontend/src/App.tsx`：

4a. 顶部 import 加 `Alert` 与 `AdminPage`：

```tsx
import { Alert, Button, Layout, Menu, Spin } from 'antd'
```

```tsx
import AdminPage from './pages/AdminPage'
```

4b. `Shell` 顶部解构加 `actAs`：

```tsx
  const { user, ready, logout, actAs } = useAuth()
```

4c. 菜单 items 改为（管理员追加「租户管理」）：

```tsx
          items={[
            { key: '/workflows', label: <Link to="/workflows">工作流</Link> },
            { key: '/datasets', label: <Link to="/datasets">数据集</Link> },
            { key: '/models', label: <Link to="/models">模型配置</Link> },
            { key: '/runs', label: <Link to="/runs">运行记录</Link> },
            ...(user.is_admin ? [{ key: '/admin', label: <Link to="/admin">租户管理</Link> }] : []),
          ]}
```

4d. `Layout.Content` 内、`<Outlet />` 之前加 impersonate 横幅：

```tsx
      <Layout.Content style={{ padding: 16 }}>
        {user.acting_as && (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                 message={`正在以 ${user.acting_as} 身份操作`}
                 action={<Button size="small" onClick={() => void actAs(null)}>返回管理员</Button>} />
        )}
        <Outlet />
      </Layout.Content>
```

4e. 在 `<Route path="/runs/:id" element={<RunDetailPage />} />` 之后加路由：

```tsx
          <Route path="/admin" element={<AdminPage />} />
```

- [ ] **Step 5: 测试 + 构建**

Run: `cd frontend && npx vitest run && npm run build`
Expected: vitest 全 PASS；构建成功。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/stores/auth.ts frontend/src/pages/AdminPage.tsx frontend/src/App.tsx
git commit -m "feat: 前端租户管理页 + act-as 身份切换 + 顶栏横幅"
```

---

## Task 13: 全量回归 + 收尾

**Files:** 无（仅验证 + 记忆）

- [ ] **Step 1: 后端全量**

Run: `cd backend && uv run pytest -q`
Expected: 全 PASS（214 既有 + 本批新增约 15 条）。

- [ ] **Step 2: 前端全量 + 构建**

Run: `cd frontend && npx vitest run && npm run build`
Expected: vitest 全 PASS（含新增 `runLog.test.ts`）；构建成功。

- [ ] **Step 3: 手验清单（口头核对，不阻塞）**

  - 自动处理节点点「✨ 用 AI 写处理代码」追加 agent 操作；分组去重指令能生成可用代码。
  - 运行详情见时间线日志、可下载 `run{id}.log`；运行列表可删除已结束运行（运行中禁用）。
  - 设 `GRAPHFLOW_ADMIN_USERS=<你的用户名>` 登录后见「租户管理」；可 act-as 某用户（顶栏横幅 + 返回管理员）、新建/删除用户；非管理员访问 `/api/admin/*` 得 403。

- [ ] **Step 4: 更新记忆**

更新 `C:\Users\Administrator\.claude\projects\E-----GraphFlow\memory\` 下相关文件：记录本批（自动处理增强 + 运行日志/删除 + admin act-as/账号增删）完成，并同步 `MEMORY.md` 索引。Task #74（admin 租户管理）标记完成。

- [ ] **Step 5: 收尾提交（若有记忆/文档改动）**

```bash
git add -A docs/
git commit -m "docs: 本批完成收尾"
```

（注意：**绝不** `git add` 项目设计.txt、.idea/、.codegraph/。）

---

## 验收标准

- 后端 `uv run pytest` 全绿；前端 `vitest run` 全绿 + `npm run build` 通过。
- 自动处理：分组去重指令可生成可用代码；AI 写代码入口显眼。
- 运行日志：节点级时间线持久化、详情页展示、可下载。
- 运行删除：单条级联清空行/状态/日志/版本/导出文件；运行中拒删。
- admin：act-as 以目标用户身份完整查看+修改；账号增删级联清理；非管理员无法 act-as 或访问 `/api/admin`（隔离红线）。
