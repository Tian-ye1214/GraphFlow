# 两套 RedLotus 硬打断按钮设计

日期：2026-06-26

## 背景与问题

两套 RedLotus Agent 都可能长时间工作而**无法被用户操控**：

- **顶层 RedLotus（AgentDrawer 工作区对话）**：由 `turn_manager`（`backend/app/agent/turns.py`）
  在后台 `asyncio.Task` 上跑；一个回合里协调者 → 管理者 → 最多 15 波 × 3 并发 worker，可跑很久。
  现有「停止并清空队列」按钮（`AgentDrawer.tsx:284` → `POST /sessions/{sid}/stop` → `request_stop`）
  是**协作式**的，只在回合之间 / 目标自动续轮之间检查（`turns.py:162,204,221`），
  **停不动正在进行的那一整个 `agent.run`**。普通单条对话回合更是完全打断不了。
- **节点内 RedLotus（节点助手）**：`POST /api/agent/node-assist`
  （`agent.py:360-387`）→ `generate_node_config`（`codegen.py:85-111`）里一次裸 `await agent.run(...)`，
  **无状态、非流式、无任何取消机制**，连 `Request` 都没收 → 关掉面板它照样在服务器上
  跑到模型自己结束。**完全无法打断。**

前端 `api/client.ts` 全局无 `AbortController`，任何在途请求都无法客户端中止。

## 用户确定的决策

1. **硬打断（立即中止）**：`task.cancel()` 当场中断正在进行的模型请求 / 工具调用，Agent 立即停下。
   已写入的图改动 / 工具结果保留；正写一半未提交的那一个工具调用随其 DB 会话上下文管理器回滚。
   **不做事务回滚**（KISS，用户已认可「已写入的改动保留」）。
2. **顶层另加独立「打断」按钮**：保留现有「停止并清空队列」（协作式）不动，旁边新增硬打断按钮。
3. **不加** `usage_limits` / 超时兜底——用户要的是手动按钮，自动上限属未被要求的防御性代码（KISS），不引入。
4. **前端排版稳定**：打断按钮出现 / 输入变化都**不得挤压**既有控件，整洁美观。

## 架构现状（约束与可复用点）

- `turn_manager.cancel(sid)`（`turns.py:71-75`）**已实现**硬中断：清队列 + `task.cancel()`。
  目前仅接在删除会话上（`agent.py:286/301`），**未接到任何停止按钮**。
  本设计让其**返回 bool**（是否真取消了一个未完成的活任务）——删除会话两处调用忽略返回值，向后兼容。
- `_run_turn` / `_run_goal` 的 `except Exception`（`turns.py:174,237`）**抓不到** `CancelledError`
  （后者是 `BaseException` 子类）→ 取消会干净穿过到 `finally: _persist_and_finish`
  （回写 history、置 idle、广播 `turn_done`）。删除会话路径已验证这条传播链。
- httpx 在途请求随注入的 `CancelledError` 中止——本仓引擎 `_cancellable`（`runner.py:30-46`）
  与删除会话已依赖同一语义，无需再造。
- 节点助手端点用**请求级临时 Agent**、无 session、无句柄、无 `call_id`；
  其工具各自用 `get_session_factory()` 开独立 DB 会话，不依赖请求 `session`。
- Starlette **不会因客户端断连自动取消** handler（探查确认），故「关页面取消」不可靠 →
  节点助手必须有**显式 stop 端点**，不能只靠客户端断连。
- 前端节点助手多轮历史存在 `localStorage`（`nodeAssistantStore.ts`，后端无状态）。

## 设计

### 1. 顶层 RedLotus —— 复用既有硬中断，端点写 marker

**后端**：新增 `POST /api/agent/sessions/{sid}/interrupt`

```python
@router.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid, user, session):
    await _get_owned(sid, user, session)          # 鉴权（沿用既有 helper）
    interrupted = turn_manager.cancel(sid)         # 已存在的 task.cancel() 硬中断，返回是否真取消了活任务
    if interrupted:                                # 仅在确实打断了一个在跑的回合时落 marker（避免对已空闲会话写多余消息）
        session.add(AgentMessage(session_id=sid, role="assistant",
                                 content_json=json.dumps({"text": "（已被用户打断）"}, ensure_ascii=False)))
        await session.commit()
        publish(user.id, "agent", sid, kind="message")
    return {"ok": True, "interrupted": interrupted}
```

- marker 由**端点自己**在正常（未被取消）的协程里写库 → **避开「在被取消的协程里 await 落库」的脆弱性**。
- 被取消的回合 `finally: _persist_and_finish` 仍照常落 history + 广播 `turn_done`，
  前端 `turn_done` 处理器刷新会话 → 见 idle → 隐藏运行态 UI；`message` 事件即时显示 marker。
- 现有 `/stop`（`request_stop`，协作式）保持不变。

**前端**（`AgentDrawer.tsx`）：运行态行 `<Space>[Tag][停止并清空队列][打断]</Space>` 新增 `interrupt()`：

```ts
const interrupt = async () => {
  if (sessionIdRef.current) {
    await api.post(`/api/agent/sessions/${sessionIdRef.current}/interrupt`)
    message.success('已打断当前回合')
  }
}
```

### 2. 节点内 RedLotus —— 新建可取消句柄 + 显式 stop 端点

**新增 `backend/app/agent/node_assist.py`**：进程内单例登记表（**按 user 隔离**）。

```python
import asyncio

class NodeAssistRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[int, asyncio.Task]] = {}   # call_id -> (user_id, task)

    def register(self, call_id, user_id, task): self._entries[call_id] = (user_id, task)
    def discard(self, call_id): self._entries.pop(call_id, None)

    def cancel(self, call_id, user_id) -> bool:
        e = self._entries.get(call_id)
        if e and e[0] == user_id and not e[1].done():   # 校验 user_id：防跨租户用泄漏的 call_id 取消他人调用
            e[1].cancel(); return True
        return False

node_assist_registry = NodeAssistRegistry()
```

**`NodeAssistIn` 加 `call_id: str = ""`**（可选，向后兼容；无 id 调用照跑，只是不可打断）。

**改 `node_assist` 端点**：把 `generate_node_config` 包成可取消子任务（`log_context` 移进子任务内，
随 task 上下文隔离）。

```python
async def _run():
    with log_context(user_id=user.id, workflow_id=body.workflow_id,
                     node_id=body.node_id, source="assistant"):
        return await codegen_mod.generate_node_config(...)

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
        task.cancel()           # 兜底取消未完成子任务，防孤儿（如 handler 自身被取消）
return {"reply": r["reply"], "config": r["config"], "sample_source": source}
```

**新增 `POST /api/agent/node-assist/stop`**：

```python
class NodeAssistStopIn(BaseModel):
    call_id: str

@router.post("/node-assist/stop")
async def node_assist_stop(body, user):
    node_assist_registry.cancel(body.call_id, user.id)
    return {"ok": True}
```

**前端**：
- `client.ts`：`post` 加可选第三参 `signal` 透传给 `fetch`（全仓首次引入 `AbortController`）。
- `nodeAssistantStore.ts`：
  - 模块级 `Map<string, AbortController>` 与 `Map<string, string>`（call_id）按 `key` 暂存在途句柄
    （**不进持久化 state**，AbortController 不可序列化）。
  - `sendAssist`：生成 `call_id`、建 `AbortController`；body 带 `call_id`、传 `signal`；`finally` 清句柄。
  - 新增 `cancelAssist(key)`：调 `POST /api/agent/node-assist/stop {call_id}` + `ctrl.abort()`，**不**直接改消息。
  - `sendAssist` 的 `catch`：判 `AbortError` → 推一条 `（已打断）` 气泡（**非 error 态**）、`pending=false`；
    非 abort 才走原「出错：…」分支。（中止与落消息统一收口在 `sendAssist` 的 promise，避免双气泡。）
- `NodeConfigForm.tsx`：见下「前端排版」。

### 3. 打断后的语义（两者一致）

硬中断 = 当场停。已提交的工具 / 图改动保留；正写一半未提交的那个工具调用随会话上下文管理器回滚；
不做事务回滚。顶层留持久化 marker `（已被用户打断）`；节点助手历史在 localStorage，由前端推 `（已打断）` 气泡。

### 4. 前端排版（稳定、不被挤压）

- **节点助手动作行**：`发送` 按钮在 `st.pending` 时**原地切换**为 `打断`（danger）——同一槽位、
  同宽度（`发送`/`打断` 均 2 个中文字符），按钮区**零重排**；不新增第二个按钮挤占宽度。
  消息区已有的 `<Spin>`（`NodeConfigForm.tsx:422`）继续表示「工作中」。
- **节点助手消息滚动区**：由 `maxHeight:200` 改为**固定高度**（含 `overflowY:auto`），
  使面板外框稳定——消息增减 / 输入变化都不把输入框、模型下拉、按钮挤上挤下。
- **顶层运行态行**：`停止并清空队列`(default/ghost) 与 `打断`(danger 实心) 视觉区分，
  放进带 `wrap` 的 `Space`，窄抽屉换行而非压扁。

## 错误处理 / 边界

- 顶层非运行态 `cancel()` 返回 False（不写 marker）；双击打断第二下亦返回 False（任务已在取消中）→ 不重复落 marker。
- 节点助手无 `call_id` 的旧调用不可打断但功能与返回不变（向后兼容）。
- 子任务用自己的 DB 会话，不与请求 `session` 并发争用；`mc` 仅读已加载列属性，请求 session 在
  `await task` 期间保持开启。
- 跨租户 `cancel` 被 `user_id` 校验拦死（对齐本项目跨租户隔离纪律）。
- 节点助手同一节点并发提交极少（UI `pending` 守卫 + 唯一 `call_id`）；不同 `call_id` 互不误伤。

## 测试（TDD，先红后绿）

**后端**
- 顶层 `interrupt`：假长回合（job 卡在 `asyncio.Event`）→ 调端点 → 子任务取消、会话置 idle、
  落 `（已被用户打断）` marker、广播。
- 节点助手 `stop`：monkeypatch `generate_node_config` 卡在 `Event` → 带 `call_id` 起 node-assist →
  调 stop → 子任务取消、端点返回 `cancelled`。
- 跨租户：bob 用 alice 的 `call_id` 调 stop → 不取消（`cancel` 返回 False / alice 调用继续）。
- 向后兼容：无 `call_id` 的 node-assist 正常返回。
- `NodeAssistRegistry` 单测：register / cancel（user 校验）/ discard / done 任务不重复取消。

**前端**
- `sendAssist` 带 `call_id` + `signal`；`cancelAssist` 调 stop 端点 + abort；abort 后推 `（已打断）`
  气泡且 `pending=false`。
- 既有 `nodeAssistantStore.test.ts` / `auditFixes.test.tsx` / `NodeConfigForm.test.tsx` 全绿。

## 范围 / 非目标

- 仅加「硬打断」能力与按钮；不改两套 Agent 的执行逻辑、工具集、流式协议。
- 不引入自动 `usage_limits` / 超时兜底。
- 不为节点助手引入服务端会话持久化（多轮仍客户端驱动）。
- 取舍：节点助手选「显式 stop 端点 + 句柄登记表」而非「客户端断连 + `request.is_disconnected`」——
  后者在本仓被证实不可靠。
