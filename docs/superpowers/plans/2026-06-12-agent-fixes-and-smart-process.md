# Agent 修复与智能处理操作 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Agent 用户绑定 bug 与登录登出缺失，自动处理节点接入 Agent 代码生成（智能处理操作），Agent 面板改底部终端式，重写 README 教程。

**Architecture:** 六块独立改动：① auth 登出端点+前端用户栏；② `session_dir()` 绝对化 + `gf login` 硬拦（根因修复）；③ 子进程代码执行器 `engine/pycode.py`（codegen 试跑与运行时共用）；④ `nodes.apply_operations_with_agent` 接入 runner；⑤ `agent/codegen.py`（临时单 Agent 生成+修复循环+上游样本采集）+ `/api/agent/codegen` 端点；⑥ 前端（智能处理 op 表单、底部面板、用户栏）+ README。

**Tech Stack:** FastAPI + SQLAlchemy async + pydantic-ai 1.107（FunctionModel 测试桩）+ React 18 / antd v5。

**执行约定（每个任务都适用）：**
- 后端测试在 `backend/` 目录：`uv run pytest <file> -q`；全量 `uv run pytest -q`（基线 **177 passed**）。
- 前端在 `frontend/` 目录：`npx vitest run`（基线 **10 passed**）、`npm run build`。
- KISS 硬规则：不加计划外防御代码、不加投机抽象。
- 提交一律两个 `-m`：第二个为 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。绝不 `git add` 项目设计.txt、`.idea/`。
- PowerShell 5.1：命令用 `;` 分隔，不可用 `&&`。
- 计划如与现有代码 API 冲突跑不通：做最小修正并在汇报中说明。

**关键已知事实：**
- pydantic-ai：`create_agent(model, tools, instructions)`（`app/agent/factory.py:25`）接受 ModelConfig 或现成 Model；`FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("...")]))` 非流式即可（codegen 不传 event_stream_handler）。
- `run_subprocess(args, *, shell, cwd, env=None, timeout)`（`app/agent/subproc.py:33`）超时抛 `subprocess.TimeoutExpired` 并杀进程树。
- conftest fixtures：`client`（tmp data_dir + ASGI）、`auth_client`（已登录 tester）、`session_factory`。
- `tests/test_agent_api.py` 已有 `mc_id` fixture（建模型配置返回 id）。
- 工作流当前图在 `Workflow.graph_json`；运行版本图在 `WorkflowVersion.graph_json`；`Run.workflow_version_id` 关联。
- 前端 `api.post<T>(path, body)`（`src/api/client.ts`）；错误抛 `ApiError(message=detail)`。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/routers/auth.py` | 改 | +logout 端点 |
| `backend/app/agent/turns.py` | 改 | session_dir 绝对化 |
| `backend/app/agent/tools.py` | 改 | +GF_LOGIN_RE 拦截 |
| `backend/app/engine/pycode.py` | 建 | 子进程执行智能处理代码（文件传参，超时杀树） |
| `backend/app/engine/pycode_harness.py` | 建 | 子进程内的执行壳（exec 用户代码、调 process） |
| `backend/app/engine/nodes.py` | 改 | +apply_operations_with_agent |
| `backend/app/engine/runner.py` | 改 | auto_process 分支改调新函数 |
| `backend/app/agent/codegen.py` | 建 | 代码生成 Agent + 修复循环 + 样本采集 |
| `backend/app/routers/agent.py` | 改 | +POST /api/agent/codegen |
| `frontend/src/stores/auth.ts` | 改 | +logout |
| `frontend/src/App.tsx` | 改 | Sider 底部用户栏 |
| `frontend/src/api/types.ts` | 改 | +CodegenOut |
| `frontend/src/canvas/forms/NodeConfigForm.tsx` | 改 | +智能处理 op 表单（AgentOpFields） |
| `frontend/src/pages/CanvasPage.tsx` | 改 | 传 workflowId/nodeId |
| `frontend/src/agent/AgentDrawer.tsx` | 改 | 底部终端式 |
| `README.md` | 改 | 分步教程重写 |

---

### Task 1: 登出端点 + 前端用户栏

**Files:**
- Modify: `backend/app/routers/auth.py`
- Modify: `frontend/src/stores/auth.ts`、`frontend/src/App.tsx`
- Test: `backend/tests/test_auth.py`（追加）

- [ ] **Step 1: 追加失败测试**（`backend/tests/test_auth.py` 末尾）

```python
async def test_logout(client):
    await client.post("/api/auth/login", json={"username": "tester"})
    assert (await client.get("/api/me")).status_code == 200
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    assert (await client.get("/api/me")).status_code == 401
```

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_auth.py -q` → FAIL（405，路由不存在）。

- [ ] **Step 3: 实现端点**（`backend/app/routers/auth.py`，加在 `login` 之后）

```python
@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
```

- [ ] **Step 4: 跑测通过**

`uv run pytest tests/test_auth.py -q` → 全部通过。

- [ ] **Step 5: 前端 auth store 加 logout**（`frontend/src/stores/auth.ts`）

接口加 `logout: () => Promise<void>`，实现：

```ts
  logout: async () => {
    await api.post('/api/auth/logout')
    set({ user: null })
  },
```

- [ ] **Step 6: Sider 底部用户栏**（`frontend/src/App.tsx`）

`Shell` 里取出 `logout`：`const { user, ready, logout } = useAuth()`。antd v5 的 `Layout.Sider` 自身是 `position: relative`，在 `</Menu>` 之后、`</Layout.Sider>` 之前加：

```tsx
        <div style={{ position: 'absolute', bottom: 16, left: 16, right: 16,
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: '#666' }}>{user.display_name || user.username}</span>
          <Button size="small" onClick={() => void logout()}>退出</Button>
        </div>
```

`Button` 加入 antd import。`user` 置 null 后既有的 `<Navigate to="/login" />` 自动生效。若 `UserInfo` 类型缺 `display_name` 字段则在 `src/api/types.ts` 补上（后端 `/api/me` 一直返回）。

- [ ] **Step 7: 前端验证**

`npx vitest run` → 10 passed；`npm run build` → 通过。

- [ ] **Step 8: 提交**

```powershell
git add backend/app/routers/auth.py backend/tests/test_auth.py frontend/src/stores/auth.ts frontend/src/App.tsx frontend/src/api/types.ts
git commit -m "feat: 登出端点 + 侧边栏当前用户显示与退出按钮" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

（types.ts 未改动则不加。）

---

### Task 2: session_dir 绝对化 + gf login 硬拦（bug 2 根因修复）

**Files:**
- Modify: `backend/app/agent/turns.py:18-19`、`backend/app/agent/tools.py`
- Test: `backend/tests/test_agent_turns.py`、`backend/tests/test_agent_tools.py`（各追加）

- [ ] **Step 1: 追加失败测试**

`backend/tests/test_agent_turns.py` 末尾（文件已 import `settings` 和 `turns`）：

```python
def test_session_dir_absolute_under_relative_data_dir(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(settings, "data_dir", Path("data"))  # 生产默认就是相对路径
    p = turns.session_dir(7)
    assert p.is_absolute()  # 相对路径会被 gf 子进程按其 cwd 二次拼接（已实际踩坑）
    assert p.parts[-2:] == ("agent", "7")
```

`backend/tests/test_agent_tools.py` 末尾（沿用文件里已有的 `tk` fixture）：

```python
async def test_gf_login_intercepted(tk):
    out = await tk.run_command("gf login bob")
    assert "禁止" in out and "Return code" not in out  # 未起子进程
    out = await tk.run_command("GF LOGIN bob")
    assert "禁止" in out
```

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_agent_turns.py tests/test_agent_tools.py -q` → 新增 3 断言路径 FAIL。

- [ ] **Step 3: 实现**

`backend/app/agent/turns.py` 的 `session_dir` 改为：

```python
def session_dir(session_id: int) -> Path:
    # 必须绝对：相对路径会被 gf 子进程按其 cwd 二次拼接（GF_STATE_FILE 失效→Agent 自行 login 成幽灵用户）
    return (settings.data_dir / "agent" / str(session_id)).resolve()
```

`backend/app/agent/tools.py`：在 `GF_DELETE_RE` 定义旁加：

```python
GF_LOGIN_RE = re.compile(r"gf\s+login\b", re.IGNORECASE)
```

在 `run_command` 里 `GF_DELETE_RE` 拦截分支的紧邻位置（之前或之后均可，照抄其结构）加：

```python
        if GF_LOGIN_RE.search(command):
            return "会话已绑定当前用户，禁止用 gf login 切换身份；直接执行业务命令即可。"
```

- [ ] **Step 4: 跑测通过 + 全量**

`uv run pytest tests/test_agent_turns.py tests/test_agent_tools.py -q` → 通过；`uv run pytest -q` → 零回归（新增 turns 1 个 + tools 1 个测试函数）。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/turns.py backend/app/agent/tools.py backend/tests/test_agent_turns.py backend/tests/test_agent_tools.py
git commit -m "fix: 会话目录绝对化修复 GF_STATE_FILE 相对路径二次拼接，硬拦 gf login 防幽灵用户" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: pycode 子进程执行器 + harness

**Files:**
- Create: `backend/app/engine/pycode.py`、`backend/app/engine/pycode_harness.py`
- Test: `backend/tests/test_pycode.py`

- [ ] **Step 1: 写失败测试**（`backend/tests/test_pycode.py`）

```python
import pytest

from app.engine.pycode import run_process_code

ROWS = [{"q": "你好", "n": 1}, {"q": "world", "n": 2}]


async def test_transforms_rows():
    code = "def process(rows):\n    return [{**r, 'q_len': len(r['q'])} for r in rows]"
    out = await run_process_code(code, ROWS)
    assert out == [{"q": "你好", "n": 1, "q_len": 2}, {"q": "world", "n": 2, "q_len": 5}]


async def test_user_print_does_not_corrupt_output():
    code = "def process(rows):\n    print('调试输出')\n    return rows"
    assert await run_process_code(code, ROWS) == ROWS


async def test_empty_code_rejected():
    with pytest.raises(ValueError, match="未生成代码"):
        await run_process_code("  ", ROWS)


async def test_missing_process_fn_fails():
    with pytest.raises(ValueError, match="执行失败"):
        await run_process_code("x = 1", ROWS)


async def test_bad_return_type_fails():
    with pytest.raises(ValueError, match="执行失败"):
        await run_process_code("def process(rows):\n    return 42", ROWS)


async def test_exception_surfaces_traceback():
    with pytest.raises(ValueError, match="boom"):
        await run_process_code("def process(rows):\n    raise RuntimeError('boom')", ROWS)


async def test_timeout_kills(monkeypatch):
    import app.engine.pycode as pc
    monkeypatch.setattr(pc, "CODE_TIMEOUT", 3)
    code = "import time\ndef process(rows):\n    time.sleep(60)\n    return rows"
    with pytest.raises(ValueError, match="超时"):
        await run_process_code(code, ROWS)
```

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_pycode.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 harness**（`backend/app/engine/pycode_harness.py`）

```python
"""智能处理代码执行壳（子进程内运行）：argv = 代码文件 输入JSON 输出JSON。
用户代码必须定义 process(rows: list[dict]) -> list[dict]。结果写文件而非 stdout，
用户代码里的 print 不会污染结果通道。"""
import json
import sys
import traceback


def main() -> int:
    code_path, in_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(code_path, encoding="utf-8") as f:
        code = f.read()
    with open(in_path, encoding="utf-8") as f:
        rows = json.load(f)
    ns: dict = {}
    try:
        exec(compile(code, "agent_code.py", "exec"), ns)
        fn = ns.get("process")
        if not callable(fn):
            raise ValueError("代码未定义 process(rows) 函数")
        out = fn(rows)
        if not isinstance(out, list) or not all(isinstance(r, dict) for r in out):
            raise ValueError("process 必须返回 list[dict]")
    except Exception:
        traceback.print_exc()
        return 1
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 实现执行器**（`backend/app/engine/pycode.py`）

```python
"""在子进程中执行智能处理代码：进程隔离（死循环拖不垮事件循环）+ 超时杀进程树。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.agent.subproc import run_subprocess

HARNESS = Path(__file__).resolve().parent / "pycode_harness.py"
CODE_TIMEOUT = 120


async def run_process_code(code: str, rows: list[dict]) -> list[dict]:
    if not code.strip():
        raise ValueError("智能处理操作未生成代码")
    with tempfile.TemporaryDirectory() as td:
        code_p, in_p, out_p = Path(td) / "code.py", Path(td) / "in.json", Path(td) / "out.json"
        code_p.write_text(code, encoding="utf-8")
        in_p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        try:
            _out, err, rc = await run_subprocess(
                [sys.executable, str(HARNESS), str(code_p), str(in_p), str(out_p)],
                shell=False, cwd=td, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=CODE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError(f"智能处理代码执行超时（{CODE_TIMEOUT} 秒）")
        if rc != 0:
            raise ValueError(f"智能处理代码执行失败:\n{err[-2000:]}")
        return json.loads(out_p.read_text(encoding="utf-8"))
```

- [ ] **Step 5: 跑测通过**

`uv run pytest tests/test_pycode.py -q` → 7 passed（超时用例约 3 秒）。

- [ ] **Step 6: 提交**

```powershell
git add backend/app/engine/pycode.py backend/app/engine/pycode_harness.py backend/tests/test_pycode.py
git commit -m "feat: 智能处理代码子进程执行器——文件传参、超时杀树、print 不污染结果" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 运行时接入——apply_operations_with_agent

**Files:**
- Modify: `backend/app/engine/nodes.py`、`backend/app/engine/runner.py:149-150`
- Test: `backend/tests/test_auto_process.py`（追加）

- [ ] **Step 1: 追加失败测试**（`backend/tests/test_auto_process.py` 末尾；文件顶部如缺 `pytest`/`nodes` import 则补）

```python
async def test_agent_op_mixed_chain():
    rows = [{"a": "x"}, {"a": "x"}, {"a": "y"}]
    ops = [{"op": "dedup", "columns": ["a"]},
           {"op": "agent", "code": "def process(rows):\n    return [{**r, 'b': r['a'].upper()} for r in rows]"}]
    out = await nodes.apply_operations_with_agent(rows, ops)
    assert out == [{"a": "x", "b": "X"}, {"a": "y", "b": "Y"}]


async def test_agent_op_empty_code_raises():
    import pytest
    with pytest.raises(ValueError, match="未生成代码"):
        await nodes.apply_operations_with_agent([{"a": 1}], [{"op": "agent", "code": ""}])
```

注意该测试文件现有 import 风格：若用的是 `from app.engine.nodes import apply_operations` 之类，按其风格改写引用（保持文件一致性），断言不变。

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_auto_process.py -q` → AttributeError。

- [ ] **Step 3: 实现**（`backend/app/engine/nodes.py`）

顶部加 `from app.engine import pycode`。把 `apply_operations` 的循环体抽成 `_apply_one` 并新增异步版：

```python
def _apply_one(rows: list[dict], op: dict, rng) -> list[dict]:
    fn = _OPS.get(op.get("op"))
    if fn is None:
        raise ValueError(f"未知操作: {op.get('op')}")
    return fn(rows, op, rng)


def apply_operations(rows: list[dict], operations: list[dict], seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    for op in operations:
        rows = _apply_one(rows, op, rng)
    return rows


async def apply_operations_with_agent(rows: list[dict], operations: list[dict],
                                      seed: int | None = None) -> list[dict]:
    """同 apply_operations，但支持 {"op": "agent", "code": ...}（子进程执行固化代码）。"""
    rng = random.Random(seed)
    for op in operations:
        if op.get("op") == "agent":
            rows = await pycode.run_process_code(op.get("code") or "", rows)
        else:
            rows = _apply_one(rows, op, rng)
    return rows
```

`backend/app/engine/runner.py` 的 `_barrier_output` 中 `auto_process` 分支改为：

```python
    if node.type == "auto_process":
        return await nodes.apply_operations_with_agent(
            inputs, cfg.get("operations", []), seed=cfg.get("seed"))
```

- [ ] **Step 4: 跑测通过 + 全量**

`uv run pytest tests/test_auto_process.py tests/test_runner.py -q` → 通过；`uv run pytest -q` 零回归。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/engine/nodes.py backend/app/engine/runner.py backend/tests/test_auto_process.py
git commit -m "feat: 自动处理节点支持 agent 操作——固化代码经子进程执行，可与纯函数操作混排" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: codegen 模块——生成 + 修复循环 + 样本采集

**Files:**
- Create: `backend/app/agent/codegen.py`
- Test: `backend/tests/test_agent_codegen.py`

- [ ] **Step 1: 写失败测试**（`backend/tests/test_agent_codegen.py`）

```python
import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from app.agent import codegen
from app.models import Dataset, DatasetRow, Run, RunRow, Workflow, WorkflowVersion

GOOD = "def process(rows):\n    return [{**r, 'ok': True} for r in rows]"
BAD = "def process(rows):\n    raise RuntimeError('炸了')"


def test_strip_code_fences():
    assert codegen.strip_code_fences(f"```python\n{GOOD}\n```") == GOOD
    assert codegen.strip_code_fences(GOOD) == GOOD


async def test_generate_no_sample_skips_preview():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(GOOD)]))
    code, preview, error = await codegen.generate_with_repair(model, "加 ok 列", [])
    assert code == GOOD and preview is None and error is None


async def test_generate_with_preview():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```python\n{GOOD}\n```")]))
    code, preview, error = await codegen.generate_with_repair(model, "加 ok 列", [{"a": 1}])
    assert error is None and preview == [{"a": 1, "ok": True}]


async def test_repair_loop_fixes_bad_code():
    calls = []

    def fn(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart(BAD if len(calls) == 1 else GOOD)])

    code, preview, error = await codegen.generate_with_repair(FunctionModel(fn), "x", [{"a": 1}])
    assert len(calls) == 2 and error is None and preview == [{"a": 1, "ok": True}]


async def test_repair_exhausted_returns_error():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(BAD)]))
    code, preview, error = await codegen.generate_with_repair(model, "x", [{"a": 1}])
    assert preview is None and "炸了" in error


def _graph(dataset_id: int) -> str:
    return json.dumps({
        "nodes": [{"id": "input_1", "type": "input", "config": {"dataset_ids": [dataset_id]}},
                  {"id": "auto_process_1", "type": "auto_process", "config": {}}],
        "edges": [{"source": "input_1", "target": "auto_process_1"}]})


async def test_sample_from_dataset_fallback(client, session_factory):
    async with session_factory() as s:
        ds = Dataset(user_id=1, name="d")
        s.add(ds)
        await s.commit()
        s.add_all([DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": i}))
                   for i in range(8)])
        wf = Workflow(user_id=1, name="w", graph_json=_graph(ds.id))
        s.add(wf)
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "auto_process_1")
    assert source == "dataset" and len(rows) == 5 and rows[0] == {"q": 0}


async def test_sample_prefers_last_run(client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=1, name="w", graph_json=_graph(999))
        s.add(wf)
        await s.commit()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=_graph(999))
        s.add(ver)
        await s.commit()
        run = Run(user_id=1, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.commit()
        s.add(RunRow(run_id=run.id, node_id="input_1", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "来自上次运行"}])))
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "auto_process_1")
    assert source == "last_run" and rows == [{"q": "来自上次运行"}]


async def test_sample_none_when_node_missing(client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=1, name="w")
        s.add(wf)
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "不存在的节点")
    assert source == "none" and rows == []
```

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_agent_codegen.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现**（`backend/app/agent/codegen.py`）

```python
"""智能处理操作的代码生成：临时单 Agent（零工具、零历史、请求级生命周期）+ 试跑修复循环 + 上游样本采集。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.factory import create_agent
from app.engine.graph import Graph, parse_graph, upstream_ids
from app.engine.pycode import run_process_code
from app.models import DatasetRow, Run, RunRow, Workflow, WorkflowVersion

SAMPLE_N = 5
MAX_REPAIR_ROUNDS = 3

INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
硬性要求：
- 只输出 Python 源码，不要任何解释或 markdown 围栏。
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。"""


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _user_prompt(instruction: str, sample_rows: list[dict]) -> str:
    sample = (json.dumps(sample_rows, ensure_ascii=False) if sample_rows
              else "（无样本，按指令中提到的列名处理）")
    return f"用户指令：{instruction}\n\n样本行：\n{sample}"


async def generate_with_repair(model, instruction: str, sample_rows: list[dict]):
    """返回 (code, preview_rows|None, error|None)。有样本时试跑并自动修复，最多 MAX_REPAIR_ROUNDS 轮。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, sample_rows))
    code = strip_code_fences(str(result.output or ""))
    if not sample_rows:
        return code, None, None
    history = result.all_messages()
    for _ in range(MAX_REPAIR_ROUNDS):
        try:
            return code, await run_process_code(code, sample_rows), None
        except ValueError as e:
            result = await agent.run(f"试跑报错，修复后重新输出完整源码：\n{e}",
                                     message_history=history)
            history = result.all_messages()
            code = strip_code_fences(str(result.output or ""))
    try:
        return code, await run_process_code(code, sample_rows), None
    except ValueError as e:
        return code, None, str(e)


async def gather_sample_rows(s: AsyncSession, workflow_id: int, node_id: str):
    """按优先级取样本：最近一次运行的上游输出 → 上游 input 数据集头部 → 无。返回 (rows, source)。"""
    run = (await s.execute(select(Run).where(Run.workflow_id == workflow_id)
                           .order_by(Run.id.desc()))).scalars().first()
    if run is not None:
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        rows = await _upstream_run_rows(s, run.id, parse_graph(ver.graph_json), node_id)
        if rows:
            return rows[:SAMPLE_N], "last_run"
    wf = await s.get(Workflow, workflow_id)
    rows = await _upstream_dataset_rows(s, parse_graph(wf.graph_json), node_id)
    if rows:
        return rows[:SAMPLE_N], "dataset"
    return [], "none"


async def _upstream_run_rows(s, run_id: int, graph: Graph, node_id: str) -> list[dict]:
    if node_id not in {n.id for n in graph.nodes}:
        return []
    out: list[dict] = []
    for uid in upstream_ids(graph, node_id):
        recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == uid, RunRow.status == "done")
            .order_by(RunRow.row_idx).limit(SAMPLE_N))).scalars().all()
        for r in recs:
            out.extend(json.loads(r.data_json))
        if len(out) >= SAMPLE_N:
            break
    return out


async def _upstream_dataset_rows(s, graph: Graph, node_id: str) -> list[dict]:
    by_id = {n.id: n for n in graph.nodes}
    if node_id not in by_id:
        return []
    seen: set[str] = set()
    frontier, dataset_ids = [node_id], []
    while frontier:
        for uid in upstream_ids(graph, frontier.pop()):
            if uid in seen:
                continue
            seen.add(uid)
            frontier.append(uid)
            if by_id[uid].type == "input":
                dataset_ids.extend(by_id[uid].config.get("dataset_ids", []))
    out: list[dict] = []
    for ds_id in dataset_ids:
        recs = (await s.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds_id)
                                .order_by(DatasetRow.idx).limit(SAMPLE_N))).scalars().all()
        out.extend(json.loads(r.data_json) for r in recs)
        if len(out) >= SAMPLE_N:
            break
    return out
```

- [ ] **Step 4: 跑测通过**

`uv run pytest tests/test_agent_codegen.py -q` → 8 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/codegen.py backend/tests/test_agent_codegen.py
git commit -m "feat: 智能处理代码生成——临时单 Agent、试跑修复循环、上游样本采集（上次运行/数据集回退）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: POST /api/agent/codegen 端点

**Files:**
- Modify: `backend/app/routers/agent.py`
- Test: `backend/tests/test_agent_api.py`（追加）

- [ ] **Step 1: 追加失败测试**（`backend/tests/test_agent_api.py` 末尾；`mc_id` fixture 已有）

```python
async def test_codegen_endpoint(auth_client, mc_id, monkeypatch):
    from app.routers import agent as agent_router

    async def fake(model, instruction, sample_rows):
        assert instruction == "去重"
        return "def process(rows):\n    return rows", [], None

    monkeypatch.setattr(agent_router, "generate_with_repair", fake)
    wid = (await auth_client.post("/api/workflows", json={"name": "w1"})).json()["id"]
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wid, "node_id": "auto_process_1",
        "instruction": "去重", "model_config_id": mc_id})
    assert r.status_code == 200
    body = r.json()
    assert body["code"].startswith("def process") and body["sample_source"] == "none"


async def test_codegen_ownership(auth_client, mc_id):
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": 9999, "node_id": "x", "instruction": "y", "model_config_id": mc_id})
    assert r.status_code == 404
    wid = (await auth_client.post("/api/workflows", json={"name": "w2"})).json()["id"]
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wid, "node_id": "x", "instruction": "y", "model_config_id": 9999})
    assert r.status_code == 422
```

- [ ] **Step 2: 跑测确认失败**

`uv run pytest tests/test_agent_api.py -q` → 新用例 FAIL（405/AttributeError）。

- [ ] **Step 3: 实现**（`backend/app/routers/agent.py`）

imports 增加：

```python
from app.agent.codegen import gather_sample_rows, generate_with_repair
```

并在 `from app.models import ...` 行加入 `Workflow`。文件末尾加：

```python
class CodegenIn(BaseModel):
    workflow_id: int
    node_id: str
    instruction: str
    model_config_id: int


@router.post("/codegen")
async def codegen(body: CodegenIn, user: User = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    mc = await session.get(ModelConfig, body.model_config_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=422, detail="模型配置无效")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="指令不能为空")
    sample_rows, source = await gather_sample_rows(session, body.workflow_id, body.node_id)
    code, preview, error = await generate_with_repair(mc, body.instruction, sample_rows)
    return {"code": code, "preview_rows": preview, "sample_source": source, "error": error}
```

- [ ] **Step 4: 跑测通过 + 全量**

`uv run pytest tests/test_agent_api.py -q` → 通过；`uv run pytest -q` 零回归。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/routers/agent.py backend/tests/test_agent_api.py
git commit -m "feat: /api/agent/codegen 端点——归属校验、样本采集、生成结果与试跑预览" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 前端智能处理操作表单

**Files:**
- Modify: `frontend/src/api/types.ts`、`frontend/src/canvas/forms/NodeConfigForm.tsx`、`frontend/src/pages/CanvasPage.tsx`

- [ ] **Step 1: types.ts 加类型**（末尾）

```ts
export interface CodegenOut {
  code: string
  preview_rows: Record<string, unknown>[] | null
  sample_source: 'last_run' | 'dataset' | 'none'
  error: string | null
}
```

- [ ] **Step 2: NodeConfigForm.tsx**

(a) `OP_DEFAULTS` 加 `agent: { op: 'agent', instruction: '', code: '' }`；`OP_LABELS` 加 `agent: '智能处理'`。

(b) 顶部 import 补 `CodegenOut`（type import）与 antd 现有引入即可。新增组件（放在 `OpFields` 之后）：

```tsx
function AgentOpFields({ op, update, workflowId, nodeId }: {
  op: Record<string, any>; update: (p: object) => void
  workflowId?: number; nodeId?: string
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<Record<string, unknown>[] | null>(null)
  const [info, setInfo] = useState('')
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const generate = async () => {
    if (!modelSel || !workflowId || !nodeId) return
    setBusy(true)
    setInfo('')
    try {
      const r = await api.post<CodegenOut>('/api/agent/codegen', {
        workflow_id: workflowId, node_id: nodeId,
        instruction: op.instruction, model_config_id: modelSel,
      })
      update({ code: r.code })
      setPreview(r.preview_rows)
      if (r.error) setInfo(`试跑失败：${r.error}`)
      else if (r.sample_source === 'none') setInfo('没有可用样本（先保存画布、运行一次更准），已跳过试跑')
    } catch (e) {
      setInfo((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <Input.TextArea rows={2} value={op.instruction} placeholder="自然语言指令，如：把 q 列翻译成英文存到 q_en，删掉空行"
                      onChange={(e) => update({ instruction: e.target.value })} />
      <Space style={{ margin: '8px 0' }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={setModelSel} options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={busy} disabled={!op.instruction || !modelSel}
                onClick={() => void generate()}>生成代码</Button>
      </Space>
      {info && <div style={{ color: '#d46b08', fontSize: 12, marginBottom: 4 }}>{info}</div>}
      {op.code && (
        <Input.TextArea rows={8} style={{ fontFamily: 'monospace', fontSize: 12 }} value={op.code}
                        onChange={(e) => update({ code: e.target.value })} />
      )}
      {preview && (
        <pre style={{ fontSize: 12, background: '#fafafa', padding: 8, maxHeight: 160, overflow: 'auto' }}>
          {JSON.stringify(preview, null, 2)}
        </pre>
      )}
    </div>
  )
}
```

(c) `AutoProcessForm` 签名加透传 props，渲染处区分 agent 操作：

```tsx
function AutoProcessForm({ config, onChange, workflowId, nodeId }: FormProps & {
  workflowId?: number; nodeId?: string
}) {
```

map 内把 `<OpFields op={op} update={...} />` 一行替换为：

```tsx
          {op.op === 'agent'
            ? <AgentOpFields op={op} workflowId={workflowId} nodeId={nodeId}
                             update={(p) => setOps(ops.map((o, j) => (j === i ? { ...o, ...p } : o)))} />
            : <OpFields op={op} update={(p) => setOps(ops.map((o, j) => (j === i ? { ...o, ...p } : o)))} />}
```

(d) 默认导出签名与分发：

```tsx
export default function NodeConfigForm({ type, config, onChange, workflowId, nodeId }: FormProps & {
  type: string; workflowId?: number; nodeId?: string
}) {
```

`auto_process` 分支改为 `<AutoProcessForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} />`，其余分支不动。

- [ ] **Step 3: CanvasPage.tsx 传参**（`NodeConfigForm` 调用处加两行 props）

```tsx
            workflowId={Number(id)}
            nodeId={selected.id}
```

- [ ] **Step 4: 验证**

`npx vitest run` → 10 passed；`npm run build` → 通过。

- [ ] **Step 5: 提交**

```powershell
git add frontend/src/api/types.ts frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/pages/CanvasPage.tsx
git commit -m "feat: 画布智能处理操作——指令输入、生成代码、可编辑代码框与试跑预览" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Agent 面板改底部终端式

**Files:**
- Modify: `frontend/src/agent/AgentDrawer.tsx:161`

- [ ] **Step 1: Drawer 改停靠**

`<Drawer open={open} onClose={() => setOpen(false)} width={440} mask={false}` 改为：

```tsx
      <Drawer open={open} onClose={() => setOpen(false)} placement="bottom" height="45vh" mask={false}
```

其余（title 控件行、消息区 `calc(100% - 120px)`、底部输入条）布局结构不变——全宽标题行足够容纳四个控件，「高级」不再截断。

- [ ] **Step 2: 验证**

`npx vitest run` → 10 passed；`npm run build` → 通过。

- [ ] **Step 3: 提交**

```powershell
git add frontend/src/agent/AgentDrawer.tsx
git commit -m "feat: Agent 面板改为底部终端式停靠，修复高级按钮截断" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: README 分步教程重写

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 重写**

保留现有「一键启动」「开发」「生产部署」「环境变量」章节原文不动；把「命令行工具 gf」「Agent 助手（红莲）」「测试」之前插入新章节「快速上手」，并在 Agent 章节补智能处理与底部面板说明。新「快速上手」全文：

```markdown
## 快速上手（Web 端从零到一）

以「把数据集里的中文问题翻译成英文」为例，5 分钟跑通一条流水线。

**1. 登录** — 打开 http://127.0.0.1:8000，输入任意用户名（如 `zhang`）直接进入。
左侧边栏底部显示当前用户，可随时退出换号。

**2. 配置模型** — 「模型配置」页 →「新建」：
- 名称随意（如 `通义`）；Base URL 填 OpenAI 兼容地址（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`）；
- 模型名填实际模型 ID（如 `qwen-max`）；API Key 填后加密保存，之后任何页面都不会回显。

**3. 上传数据集** — 「数据集」页 →「上传」，支持 xlsx / csv / jsonl。
上传后可点开预览，确认列名（下文用 `q` 列举例）。

**4. 搭流水线** — 「工作流」页 →「新建」→ 进入画布：
- 点「+ 输入」「+ LLM 合成」「+ 自动处理」「+ 输出」各加一个节点，拖动节点间圆点连线：输入 → LLM 合成 → 自动处理 → 输出；
- 点节点弹出配置：**输入**选数据集；**LLM 合成**选模型、User Prompt 写 `把{{q}}翻译成英文，只输出译文`、输出列名填 `q_en`；**自动处理**可加去重/过滤等操作，也可加「智能处理」操作——用自然语言描述（如 `删掉 q_en 为空的行`），选模型点「生成代码」，预览结果满意后即固化；**输出**可勾选「保存为新数据集」；
- 点「保存」。

**5. 运行** — 画布点「运行」自动跳到运行详情页：实时进度、token 统计、
失败行可单独重跑，中断后再次运行自动断点续跑。

**6. 导出** — 运行详情页选格式（xlsx / csv / jsonl）下载结果。

页面之间实时联动：CLI 或 Agent 改了工作流，已打开的画布会即时刷新
（画布有未保存改动时显示提示条而不是覆盖你的编辑）。
```

Agent 章节末尾追加一段：

```markdown
- **智能处理操作**：画布「自动处理」节点里可添加「智能处理」操作——写一句自然语言，
  Agent 看着上游样本数据生成 Python 处理代码并试跑给你预览，确认后固化进节点，
  运行时在独立子进程里执行（120 秒超时保护）。
- Agent 面板停靠在页面底部（像终端一样），右下角 ❦ 呼出。
```

- [ ] **Step 2: 提交**

```powershell
git add README.md
git commit -m "docs: README 重写为分步上手教程，补智能处理与底部面板说明" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: 收尾——脏数据清理 + 全量回归（主会话执行，不派子代理）

- [ ] **Step 1: 清理嵌套目录**：删除 `backend/data/agent/*/data` 子目录（bug 2 产物）。
- [ ] **Step 2: 幽灵用户处置**：枚举本地 DB 用户与 `alice`（id 3，Agent 误建）名下资源清单，**呈报用户决定是否删除**（README 示例也叫 alice，可能有用户自己的 CLI 数据，不得擅删）。
- [ ] **Step 3: 全量回归**：backend `uv run pytest -q`、frontend `npx vitest run` + `npm run build`，与各任务新增测试数核对。
- [ ] **Step 4: 人工验收项**（报告给用户自行操作）：登录后侧边栏见用户名；Agent 建工作流画布实时出现；智能处理操作生成→预览→运行。

---

## 验收对照（spec §7）

| 验收标准 | 任务 |
|---|---|
| 1 登出后 /api/me 401、前端显示用户 | Task 1 |
| 2 session_dir 绝对 + gf login 拦截 | Task 2 |
| 3 codegen 端点（生成/修复/无样本/归属） | Task 5、6 |
| 4 agent 操作运行时（转换/超时/坏输出/混排） | Task 3、4 |
| 5 全量零回归 | 每任务 + Task 10 |
| 6 人工验收（推送恢复、清理完毕） | Task 10 |
