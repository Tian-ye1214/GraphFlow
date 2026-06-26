# 两套 RedLotus 硬打断按钮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给顶层 RedLotus（AgentDrawer 对话回合）与节点内 RedLotus（节点助手）各加一个「硬打断」按钮，让用户能立即中止正在进行的 `agent.run`。

**Architecture:** 顶层复用既有 `turn_manager.cancel()`（`task.cancel()` 硬中断），让其返回 bool，新增 `POST /sessions/{sid}/interrupt` 端点并由端点落 marker。节点助手新建按 user 隔离的 `NodeAssistRegistry`（`call_id → (user_id, Task)`），把 `generate_node_config` 包成可取消子任务并新增 `POST /node-assist/stop` 端点；前端引入 `AbortController` 并加打断按钮。

**Tech Stack:** Python / FastAPI / SQLAlchemy async / pytest（后端）；React / TypeScript / antd / vitest + @testing-library（前端）。

## Global Constraints

- 提交信息**不得出现 claude**，**不加 Co-Authored-By 尾注**。
- 全程中文回复；提交信息用中文（沿用仓库 `feat(scope): …` 风格）。
- KISS：只加「硬打断」能力与按钮，**不引入** `usage_limits`/超时兜底等未被要求的防御性代码。
- **不引入任何 dry_run/假运行**。
- 复用优先、单点化，不堆补丁式新代码。
- 跨租户隔离：节点助手 stop 必须校验 `user_id`，他人 `call_id` 无法取消本人调用。
- 前端排版稳定：打断按钮出现 / 输入变化都**不得挤压**既有控件。
- 后端测试从 `backend/` 目录跑（`cd backend && python -m pytest …`）；前端从 `frontend/` 跑（`cd frontend && npx vitest run …`、`npx tsc --noEmit`）。
- 测试只在本地，**不推 origin**。
- 工作分支：`redlotus-interrupt`（已建，spec 已提交于 `f236d3c`）。

---

## File Structure

**后端**
- Modify `backend/app/agent/turns.py` — `cancel()` 返回 bool。
- Modify `backend/app/routers/agent.py` — 新增 `interrupt` 端点；`NodeAssistIn` 加 `call_id`；改 `node_assist` 为可取消子任务；新增 `node-assist/stop` 端点。
- Create `backend/app/agent/node_assist.py` — `NodeAssistRegistry` 单例。
- Modify `backend/tests/test_agent_turns.py` — `cancel()` bool 单测。
- Create `backend/tests/test_node_assist_registry.py` — 登记表单测。
- Create `backend/tests/test_interrupt_api.py` — 两个端点的 HTTP 测试。

**前端**
- Modify `frontend/src/api/client.ts` — `post` 支持可选 `signal`。
- Modify `frontend/src/agent/nodeAssistantStore.ts` — `call_id` + `AbortController` + `cancelAssist` + AbortError 分支。
- Modify `frontend/src/canvas/forms/NodeConfigForm.tsx` — 发送↔打断原地切换 + 消息区固定高度。
- Modify `frontend/src/agent/AgentDrawer.tsx` — `interrupt()` + 打断按钮。
- Modify `frontend/src/agent/nodeAssistantStore.test.ts` — `call_id`/`signal`/`cancelAssist` 测试。
- Modify `frontend/src/canvas/forms/NodeConfigForm.test.tsx` — 打断按钮集成测试。

---

## Task 1: `turn_manager.cancel()` 返回是否取消了活任务

**Files:**
- Modify: `backend/app/agent/turns.py:71-75`
- Test: `backend/tests/test_agent_turns.py`

**Interfaces:**
- Produces: `AgentTurnManager.cancel(session_id: int) -> bool` — 存在未完成的 `_drain` 任务并已 `task.cancel()` 时返回 `True`，否则 `False`。删除会话两处旧调用忽略返回值（向后兼容）。

- [ ] **Step 1: 写失败测试**

加到 `backend/tests/test_agent_turns.py` 末尾（沿用文件顶部已 import 的 `asyncio`、`turns`、`FakeSystem`、`sid` fixture）：

```python
async def test_cancel_returns_true_for_live_task_false_when_idle(monkeypatch, sid):
    class Blocker(FakeSystem):
        async def run_turn(self, text, history):
            await asyncio.sleep(5)
            return history, "x"

    tm = turns.AgentTurnManager()
    monkeypatch.setattr(turns, "AgentSystem", lambda **kw: Blocker([]))
    assert tm.cancel(sid) is False                 # 无在跑任务
    tm.submit(sid, 1, "go")
    await asyncio.sleep(0.01)                       # 让 _drain 任务起跑
    task = tm.tasks.get(sid)
    assert tm.cancel(sid) is True                   # 取消了活任务
    assert tm.cancel(sid) is False                  # 第二次：任务已在取消中/已移除
    import contextlib
    if task:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_turns.py::test_cancel_returns_true_for_live_task_false_when_idle -v`
Expected: FAIL（当前 `cancel` 返回 `None`，第一个 `is False` 断言即挂 / `is True` 断言失败）

- [ ] **Step 3: 改实现**

`backend/app/agent/turns.py` 把 `cancel` 改为返回 bool：

```python
    def cancel(self, session_id: int) -> bool:
        self.queues.get(session_id, deque()).clear()
        task = self.tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_agent_turns.py -v`
Expected: PASS（新测试 + 既有 turns 测试全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/turns.py backend/tests/test_agent_turns.py
git commit -m "feat(agent): turn_manager.cancel 返回是否取消了活任务"
```

---

## Task 2: 顶层 RedLotus 回合硬打断端点

**Files:**
- Modify: `backend/app/routers/agent.py`（紧接 `stop_session` 之后，约 `:173`）
- Test: `backend/tests/test_interrupt_api.py`（新建）

**Interfaces:**
- Consumes: `AgentTurnManager.cancel(sid) -> bool`（Task 1）。
- Produces: `POST /api/agent/sessions/{sid}/interrupt` → `{"ok": True, "interrupted": bool}`；`interrupted` 为 `True` 时落一条 `assistant` 消息 `{"text": "（已被用户打断）"}` 并 `publish(user.id, "agent", sid, kind="message")`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_interrupt_api.py`：

```python
import pytest

from app.agent.turns import AgentTurnManager


class StubTM:
    def __init__(self, result):
        self.result = result
        self.cancelled = []

    def cancel(self, sid):
        self.cancelled.append(sid)
        return self.result


async def _make_session(auth_client):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    sess = (await auth_client.post("/api/agent/sessions",
                                   json={"model_config_id": mc["id"]})).json()
    return sess["id"]


async def test_interrupt_writes_marker_when_cancelled(auth_client, monkeypatch):
    sid = await _make_session(auth_client)
    stub = StubTM(True)
    monkeypatch.setattr("app.routers.agent.turn_manager", stub)
    r = await auth_client.post(f"/api/agent/sessions/{sid}/interrupt")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "interrupted": True}
    assert stub.cancelled == [sid]
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["messages"][-1]["role"] == "assistant"
    assert detail["messages"][-1]["content"]["text"] == "（已被用户打断）"


async def test_interrupt_no_marker_when_idle(auth_client, monkeypatch):
    sid = await _make_session(auth_client)
    stub = StubTM(False)
    monkeypatch.setattr("app.routers.agent.turn_manager", stub)
    r = await auth_client.post(f"/api/agent/sessions/{sid}/interrupt")
    assert r.json() == {"ok": True, "interrupted": False}
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert all(m["content"].get("text") != "（已被用户打断）" for m in detail["messages"])


async def test_interrupt_unknown_session_404(auth_client):
    r = await auth_client.post("/api/agent/sessions/999999/interrupt")
    assert r.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_interrupt_api.py -v`
Expected: FAIL（404 —— 端点尚不存在）

- [ ] **Step 3: 改实现**

在 `backend/app/routers/agent.py` 的 `stop_session`（`:168-173`）之后新增：

```python
@router.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid: int, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    await _get_owned(sid, user, session)
    interrupted = turn_manager.cancel(sid)
    if interrupted:   # 仅在确实打断了一个在跑的回合时落 marker（避免对已空闲会话写多余消息）
        session.add(AgentMessage(session_id=sid, role="assistant",
                                 content_json=json.dumps({"text": "（已被用户打断）"}, ensure_ascii=False)))
        await session.commit()
        publish(user.id, "agent", sid, kind="message")
    return {"ok": True, "interrupted": interrupted}
```

（`AgentMessage`、`json`、`publish`、`_get_owned`、`turn_manager` 均已在该文件顶部 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_interrupt_api.py -v`
Expected: PASS（3 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/agent.py backend/tests/test_interrupt_api.py
git commit -m "feat(agent): 顶层 RedLotus 回合硬打断端点 + 打断 marker"
```

---

## Task 3: 节点助手可取消句柄登记表

**Files:**
- Create: `backend/app/agent/node_assist.py`
- Test: `backend/tests/test_node_assist_registry.py`（新建）

**Interfaces:**
- Produces:
  - `node_assist_registry`：`NodeAssistRegistry` 单例。
  - `register(call_id: str, user_id: int, task: asyncio.Task) -> None`
  - `discard(call_id: str) -> None`
  - `cancel(call_id: str, user_id: int) -> bool` —— call_id 存在、`user_id` 匹配且任务未完成时 `task.cancel()` 并返回 `True`；否则 `False`（含跨租户）。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_node_assist_registry.py`：

```python
import asyncio

from app.agent.node_assist import NodeAssistRegistry


async def test_cancel_matching_user_cancels_task():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    assert reg.cancel("c1", 7) is True
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_cancel_wrong_user_does_not_cancel():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    assert reg.cancel("c1", 99) is False     # 跨租户：不取消
    assert not task.done()
    task.cancel()
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_cancel_unknown_callid_false():
    reg = NodeAssistRegistry()
    assert reg.cancel("nope", 1) is False


async def test_discard_removes_entry():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    reg.discard("c1")
    assert reg.cancel("c1", 7) is False      # 已注销 → 找不到
    task.cancel()
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_node_assist_registry.py -v`
Expected: FAIL（`ModuleNotFoundError: app.agent.node_assist`）

- [ ] **Step 3: 改实现**

新建 `backend/app/agent/node_assist.py`：

```python
"""节点助手在途调用的可取消句柄登记表：call_id → (user_id, Task)。
按 user_id 隔离——他人即便拿到 call_id 也无法取消本人调用（跨租户隔离）。"""
import asyncio


class NodeAssistRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[int, asyncio.Task]] = {}

    def register(self, call_id: str, user_id: int, task: asyncio.Task) -> None:
        self._entries[call_id] = (user_id, task)

    def discard(self, call_id: str) -> None:
        self._entries.pop(call_id, None)

    def cancel(self, call_id: str, user_id: int) -> bool:
        entry = self._entries.get(call_id)
        if entry and entry[0] == user_id and not entry[1].done():
            entry[1].cancel()
            return True
        return False


node_assist_registry = NodeAssistRegistry()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_node_assist_registry.py -v`
Expected: PASS（4 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/node_assist.py backend/tests/test_node_assist_registry.py
git commit -m "feat(agent): 节点助手可取消句柄登记表（按 user 隔离）"
```

---

## Task 4: 节点助手可取消子任务 + 硬打断端点

**Files:**
- Modify: `backend/app/routers/agent.py`（`NodeAssistIn` `:349-358`、`node_assist` `:360-387`，并在其后新增 stop 端点）
- Test: `backend/tests/test_interrupt_api.py`（追加）

**Interfaces:**
- Consumes: `node_assist_registry`（Task 3）。
- Produces:
  - `NodeAssistIn` 新增字段 `call_id: str = ""`。
  - `node_assist` 在 `body.call_id` 非空时把 `generate_node_config` 注册为可取消子任务；被取消时返回 `{"reply": "（已打断）", "config": None, "sample_source": source, "cancelled": True}`。
  - `POST /api/agent/node-assist/stop`（body `{call_id: str}`）→ `{"ok": True}`，内部 `node_assist_registry.cancel(call_id, user.id)`。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_interrupt_api.py`（顶部加 `import asyncio`）：

```python
async def _make_model_and_wf(auth_client, node_id, node_type):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": {
        "nodes": [{"id": node_id, "type": node_type, "config": {}}], "edges": []}})
    return mc, wf


async def test_node_assist_stop_cancels_inflight(auth_client, monkeypatch):
    started = asyncio.Event()

    async def blocking_cfg(*a, **k):
        started.set()
        await asyncio.Event().wait()        # 永久阻塞，直到被取消

    monkeypatch.setattr("app.agent.codegen.generate_node_config", blocking_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    call_id = "call-xyz"
    post = asyncio.create_task(auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "x", "model_config_id": mc["id"], "call_id": call_id}))
    await asyncio.wait_for(started.wait(), 5)   # 确保在途且已注册
    r2 = await auth_client.post("/api/agent/node-assist/stop", json={"call_id": call_id})
    assert r2.status_code == 200
    r1 = await asyncio.wait_for(post, 5)
    assert r1.status_code == 200
    assert r1.json()["cancelled"] is True
    assert r1.json()["config"] is None


async def test_node_assist_without_callid_still_works(auth_client, monkeypatch):
    async def fake_cfg(*a, **k):
        return {"reply": "ok", "config": None}

    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "x", "model_config_id": mc["id"]})   # 无 call_id
    assert r.status_code == 200
    assert r.json()["reply"] == "ok"


async def test_node_assist_stop_unknown_callid_ok(auth_client):
    r = await auth_client.post("/api/agent/node-assist/stop", json={"call_id": "ghost"})
    assert r.status_code == 200      # 找不到也优雅返回（幂等）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_interrupt_api.py -k node_assist -v`
Expected: FAIL（`stop` 端点不存在 → 404/422；`cancelled` 字段缺失）

- [ ] **Step 3: 改实现**

在 `backend/app/routers/agent.py` 顶部 import 区加：

```python
from app.agent.node_assist import node_assist_registry
```

`NodeAssistIn`（`:349-358`）末尾加字段：

```python
    history: list[dict] = []
    call_id: str = ""
```

把 `node_assist`（`:360-387`）的 `try/except` 段替换为可取消子任务版本：

```python
    async def _run():
        with log_context(user_id=user.id, workflow_id=body.workflow_id,
                         node_id=body.node_id, source="assistant"):
            return await codegen_mod.generate_node_config(
                mc, body.node_type, body.instruction, columns, current_config=body.current_config,
                preview_tools=preview_tools, params=body.params, history=body.history)

    task = asyncio.create_task(_run())
    if body.call_id:
        node_assist_registry.register(body.call_id, user.id, task)
    try:
        r = await task
    except asyncio.CancelledError:
        return {"reply": "（已打断）", "config": None, "sample_source": source, "cancelled": True}
    except ModelHTTPError as exc:
        _raise_model_http_error(exc, mc)
    finally:
        node_assist_registry.discard(body.call_id)
        if not task.done():
            task.cancel()       # 兜底取消未完成子任务，防孤儿
    return {"reply": r["reply"], "config": r["config"], "sample_source": source}
```

在 `node_assist` 之后新增 stop 端点：

```python
class NodeAssistStopIn(BaseModel):
    call_id: str


@router.post("/node-assist/stop")
async def node_assist_stop(body: NodeAssistStopIn,
                           user: User = Depends(get_current_user)):
    node_assist_registry.cancel(body.call_id, user.id)
    return {"ok": True}
```

在文件顶部 import 区确保有 `import asyncio`（若无则加到 `import json` 旁）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_interrupt_api.py tests/test_assistant_context.py tests/test_node_assist_multiturn.py -v`
Expected: PASS（新测试 + 既有节点助手测试全绿，证明向后兼容）

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/agent.py backend/tests/test_interrupt_api.py
git commit -m "feat(agent): 节点助手硬打断端点 + 可取消子任务"
```

---

## Task 5: 前端 `api.post` 支持 AbortSignal + 节点助手打断逻辑

**Files:**
- Modify: `frontend/src/api/client.ts:26-27`
- Modify: `frontend/src/agent/nodeAssistantStore.ts`
- Test: `frontend/src/agent/nodeAssistantStore.test.ts`

**Interfaces:**
- Produces:
  - `api.post<T>(p, body?, signal?: AbortSignal)`。
  - `sendAssist` POST `/api/agent/node-assist` 时带 `call_id`（字符串）与 `signal`。
  - `cancelAssist(key: string): void` —— POST `/api/agent/node-assist/stop {call_id}` 并 `abort()` 在途请求；中止后 `sendAssist` 落一条非 error 的 `（已打断）` 气泡、`pending=false`。

- [ ] **Step 1: 写失败测试**

加到 `frontend/src/agent/nodeAssistantStore.test.ts`（`import` 区补 `cancelAssist`）：

```ts
import {
  setDraft, newConversation, switchConversation, sendAssist, activeConversation,
  cancelAssist, getState as readState,
} from './nodeAssistantStore'
```

在 `describe` 内追加：

```ts
  it('sendAssist 带 call_id 与 AbortSignal', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, 'hi')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const [url, body, signal] = (api.post as any).mock.calls[0]
    expect(url).toBe('/api/agent/node-assist')
    expect(typeof body.call_id).toBe('string')
    expect(body.call_id.length).toBeGreaterThan(0)
    expect(signal).toBeInstanceOf(AbortSignal)
  })

  it('cancelAssist 调 stop 端点 + 中止在途请求，落「（已打断）」气泡', async () => {
    let captured: AbortSignal | undefined
    ;(api.post as any).mockImplementationOnce((_u: string, _b: any, signal: AbortSignal) => {
      captured = signal
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          const e = new Error('aborted'); (e as any).name = 'AbortError'; reject(e)
        })
      })
    })
    setDraft(KEY, 'hi')
    const p = sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    await Promise.resolve()
    expect(readState(KEY).pending).toBe(true)
    ;(api.post as any).mockResolvedValueOnce({ ok: true })   // stop 端点
    cancelAssist(KEY)
    await p
    const stopCall = (api.post as any).mock.calls.find((c: any[]) => c[0] === '/api/agent/node-assist/stop')
    expect(stopCall).toBeTruthy()
    expect(stopCall[1].call_id.length).toBeGreaterThan(0)
    expect(captured?.aborted).toBe(true)
    const s = readState(KEY)
    expect(s.pending).toBe(false)
    const msgs = activeConversation(s).messages
    expect(msgs[msgs.length - 1]).toMatchObject({ role: 'assistant', text: '（已打断）' })
    expect(msgs[msgs.length - 1].error).toBeUndefined()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/agent/nodeAssistantStore.test.ts`
Expected: FAIL（`cancelAssist` 未导出；`api.post` 未传 signal/call_id）

- [ ] **Step 3: 改实现**

`frontend/src/api/client.ts` 把 `post` 改为：

```ts
  post: <T>(p: string, body?: unknown, signal?: AbortSignal) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), signal }),
```

`frontend/src/agent/nodeAssistantStore.ts` 在 `replaceConv` 之后加模块级句柄表：

```ts
const controllers = new Map<string, AbortController>()
const callIds = new Map<string, string>()
```

把 `sendAssist` 整体替换为（带 call_id、signal、AbortError 分支、finally 清句柄）：

```ts
export async function sendAssist(key: string, payload: {
  workflow_id: number; node_id: string; node_type: string; model_config_id: number
  current_config: Record<string, any>; params: Record<string, any>
}) {
  const cur = getState(key)
  const text = cur.draft.trim()
  if (!text || cur.pending) return
  const active = activeConversation(cur)
  const history = active.messages.filter((m) => !m.error).map((m) => ({ role: m.role, text: m.text }))
  const withUser: Conversation = {
    ...active,
    title: active.title || text.slice(0, 20),
    messages: [...active.messages, { role: 'user', text }],
  }
  set(key, { ...cur, draft: '', pending: true, conversations: replaceConv(cur, withUser) })
  const callId = newId()
  const ctrl = new AbortController()
  controllers.set(key, ctrl)
  callIds.set(key, callId)
  try {
    const r = await api.post<NodeAssistReply>('/api/agent/node-assist',
      { ...payload, instruction: text, history, call_id: callId }, ctrl.signal)
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    set(key, { ...c, pending: false, conversations: replaceConv(c,
      { ...a, messages: [...a.messages, { role: 'assistant', text: r.reply, config: r.config ?? undefined }] }) })
  } catch (e) {
    const aborted = (e as Error).name === 'AbortError'
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    const bubble: AssistMsg = aborted
      ? { role: 'assistant', text: '（已打断）' }
      : { role: 'assistant', text: '出错：' + (e as Error).message, error: true as const }
    set(key, { ...c, pending: false, conversations: replaceConv(c, { ...a, messages: [...a.messages, bubble] }) })
  } finally {
    controllers.delete(key)
    callIds.delete(key)
  }
}

export function cancelAssist(key: string) {
  const callId = callIds.get(key)
  if (callId) void api.post('/api/agent/node-assist/stop', { call_id: callId }).catch(() => {})
  controllers.get(key)?.abort()
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/agent/nodeAssistantStore.test.ts src/agent/auditFixes.test.tsx && npx tsc --noEmit`
Expected: PASS（新测试 + 既有 store/审计测试全绿，tsc 无报错）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/api/client.ts frontend/src/agent/nodeAssistantStore.ts frontend/src/agent/nodeAssistantStore.test.ts
git commit -m "feat(web): api.post 支持 AbortSignal + 节点助手 cancelAssist"
```

---

## Task 6: 节点助手「发送↔打断」原地切换 + 稳定排版

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`NodeAssist` 组件 `:381-435`，import 行 `:5`）
- Test: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`（追加）

**Interfaces:**
- Consumes: `cancelAssist`（Task 5）、`st.pending`。
- Produces：`st.pending` 时动作行展示 `打断` 按钮（danger，原槽位替换 `发送`，点击调 `cancelAssist(key)`）；消息滚动区固定高度。

- [ ] **Step 1: 写失败测试**

追加到 `frontend/src/canvas/forms/NodeConfigForm.test.tsx` 末尾：

```ts
describe('NodeConfigForm 节点助手打断按钮', () => {
  it('在途时「发送」原地切换为「打断」，点击调用 stop 端点', async () => {
    const posts = mockNodeConfigApis({ ls: { input: ['q'], output: ['q'] } })
    // 预置 draft + 助手模型，使「发送」可点
    persistAssistState('graphflow.nodeAssistant.v1:1:llm_synth:ls', '把 q 翻译成英文', 1)
    // node-assist 请求挂起 → 维持 pending
    let releaseHang: () => void = () => {}
    const realFetch = globalThis.fetch as any
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
      if (path.includes('/api/agent/node-assist/stop')) {
        posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
        return new Response(JSON.stringify({ ok: true }), { status: 200 })
      }
      if (path.endsWith('/api/agent/node-assist')) {
        posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
        return new Promise<Response>((resolve) => { releaseHang = () => resolve(new Response('{}', { status: 200 })) })
      }
      return realFetch(path, init)
    }))

    render(<NodeConfigForm type="llm_synth" workflowId={1} nodeId="ls" config={{}} onChange={() => {}} />)
    fireEvent.click(await screen.findByText('发送'))
    const interruptBtn = await screen.findByText('打断')
    expect(interruptBtn).toBeInTheDocument()
    expect(screen.queryByText('发送')).not.toBeInTheDocument()   // 原地替换，不并存
    fireEvent.click(interruptBtn)
    await waitFor(() => {
      expect(posts.some((p) => p.path.includes('/api/agent/node-assist/stop'))).toBe(true)
    })
    releaseHang()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx -t 节点助手打断按钮`
Expected: FAIL（找不到「打断」按钮）

- [ ] **Step 3: 改实现**

`frontend/src/canvas/forms/NodeConfigForm.tsx` import 行（`:5`）补 `cancelAssist`：

```ts
import { activeConversation, cancelAssist, newConversation, sendAssist, setDraft, setModelConfigId, switchConversation, useNodeAssist } from '../../agent/nodeAssistantStore'
```

把消息滚动区（`:411`）`maxHeight: 200` 改为固定高度，稳定外框：

```tsx
      <div style={{ height: 200, overflowY: 'auto', marginBottom: 8 }}>
```

把动作行（`:426-432`）的发送按钮改为「发送↔打断」原地切换：

```tsx
      <Space style={{ marginTop: 8 }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={(v) => setModelConfigId(key, v)}
                options={models.map((m) => ({ value: m.id, label: m.name }))} />
        {st.pending
          ? <Button size="small" danger onClick={() => cancelAssist(key)}>打断</Button>
          : <Button size="small" disabled={!st.draft.trim() || !modelSel} onClick={send}>发送</Button>}
      </Space>
```

（消息区已有的 `<Spin>`（`:422`）继续表示「工作中」；动作行同一槽位单按钮，按钮区零重排。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx && npx tsc --noEmit`
Expected: PASS（新测试 + 既有 NodeConfigForm 测试全绿，tsc 无报错）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx
git commit -m "feat(web): 节点助手发送↔打断原地切换 + 消息区固定高度"
```

---

## Task 7: 顶层 RedLotus 打断按钮（AgentDrawer）

**Files:**
- Modify: `frontend/src/agent/AgentDrawer.tsx`（`stop` 之后 `:163`；运行态行 `:281-286`）

**Interfaces:**
- Consumes: `POST /api/agent/sessions/{sid}/interrupt`（Task 2）。
- Produces：运行态行在「停止并清空队列」旁新增「打断」按钮（danger 实心），调 `interrupt()`。

- [ ] **Step 1: 改实现**

`frontend/src/agent/AgentDrawer.tsx` 在 `stop`（`:158-163`）之后新增：

```ts
  const interrupt = async () => {
    if (sessionIdRef.current) {
      await api.post(`/api/agent/sessions/${sessionIdRef.current}/interrupt`)
      message.success('已打断当前回合')
    }
  }
```

把运行态行（`:281-286`）的 `<Space>` 改为带 `wrap` 且两个按钮视觉区分：

```tsx
            <Space style={{ marginBottom: 6 }} wrap>
              <Tag color="processing">红莲正在工作…{goalRound > 0 && `目标进行中 · 第 ${goalRound} 轮`}</Tag>
              <Button size="small" onClick={() => void stop()}>停止并清空队列</Button>
              <Button size="small" danger type="primary" onClick={() => void interrupt()}>打断</Button>
            </Space>
```

（`停止并清空队列` 改为默认样式、`打断` 用 `danger + primary` 实心，视觉区分；`wrap` 让窄抽屉换行而非压扁。）

- [ ] **Step 2: 类型检查 + 全前端测试**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`
Expected: PASS（tsc 无报错；既有全部前端测试绿——AgentDrawer 仅新增按钮与一个调用 `api.post` 的处理函数，不改既有逻辑）

> 说明：AgentDrawer 全量渲染依赖 EventSource 等，独立渲染成本过高且收益低；端点行为已在 Task 2 以 TDD 覆盖，此处按钮为 3 行薄包装，以 tsc + 全套测试 + Task 8 真实活体验证为门禁。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/agent/AgentDrawer.tsx
git commit -m "feat(web): 顶层 RedLotus 打断按钮"
```

---

## Task 8: 全量回归 + 真实活体验证

**Files:** 无（验证任务）

- [ ] **Step 1: 后端全量**

Run: `cd backend && python -m pytest -q`
Expected: 全绿（既有 786 + 本批新增约 12 个测试，无回归）

- [ ] **Step 2: 前端全量 + 类型**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: 全绿，tsc clean

- [ ] **Step 3: 真实活体验证（需重启服务生效）**

合并/重启后人工验证（本仓惯例：真实 DeepSeek、smoke 用户建即删、回基线零损失）：

1. **顶层打断**：开一个 RedLotus 会话，发一条会触发多轮/多 worker 的指令；运行中点「打断」→ 回合**立即停止**、会话转 idle、对话末尾出现「（已被用户打断）」、模型调用不再增长。
2. **节点助手打断**：在某 llm_synth 节点助手发一条指令，趁 `<Spin>` 转时点「打断」→ 立即出现「（已打断）」气泡、`pending` 解除、服务端该次 `agent.run` 停止（model-logs 不再新增该 trace）。
3. **排版**：节点助手面板在消息增减 / 输入时按钮与输入框**不被挤压**；顶层两个按钮在窄抽屉换行不压扁。
4. **跨租户**：（可选）另一个用户用泄漏的 `call_id` 调 `node-assist/stop` → 不影响本人在途调用。

- [ ] **Step 4: 完成分支**

全绿且活体通过后，按用户偏好合并 `redlotus-interrupt` 进 master 并删除该分支（本地不推 origin）。调用 `superpowers:finishing-a-development-branch` 走收尾选择。

---

## Self-Review（计划对照 spec）

- **Spec §设计.1 顶层端点 + cancel bool + 端点写 marker** → Task 1 + Task 2 ✅
- **Spec §设计.1 顶层「打断」按钮** → Task 7 ✅
- **Spec §设计.2 NodeAssistRegistry（user 隔离）** → Task 3 ✅
- **Spec §设计.2 NodeAssistIn.call_id / 可取消子任务 / stop 端点** → Task 4 ✅
- **Spec §设计.2 前端 client.signal / store call_id+AbortController / cancelAssist** → Task 5 ✅
- **Spec §设计.4 前端排版（发送↔打断原地、消息区固定高度、顶层 wrap 区分）** → Task 6 + Task 7 ✅
- **Spec §设计.3 打断语义（marker / 「（已打断）」气泡）** → Task 2（顶层 marker）+ Task 5（节点气泡）✅
- **Spec §边界 跨租户 / 向后兼容 / 双击 / 非运行态** → Task 1（idle False）、Task 3（跨租户）、Task 4（无 call_id 兼容）✅
- **Spec §测试** → Task 1/2/3/4/5/6 的 TDD + Task 8 全量与活体 ✅
- **占位符扫描**：无 TBD/TODO；每个代码步均含完整可粘贴代码 ✅
- **类型一致性**：`cancel(sid)->bool`、`cancel(call_id,user_id)->bool`、`cancelAssist(key)`、`api.post(p,body?,signal?)` 全计划一致 ✅
