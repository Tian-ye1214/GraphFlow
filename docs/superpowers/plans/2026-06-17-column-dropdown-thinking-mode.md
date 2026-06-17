# 列 I/O 下拉框 + 三态删除 + 思考模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 列 I/O 收进下拉框、支持「透传/喂模型/删除」三态点击（删除列下游不可见）；全部 LLM 调用（跑数节点 + 全部 Agent 角色）默认开启思考、力度 high，可在节点级关闭/调力度。

**Architecture:** 思考配置由一个共享纯函数 `thinking_extra_body(params)` 产出 `extra_body`，节点路径（`services/llm.py`）与 Agent 路径（`agent/factory.py`）两路统一注入。列删除由节点 config 的 `drop_columns: list[str]` 表达：引擎在唯一落库切口 `_write_unit` 统一剔列，列血缘 `_node_output` 统一减列，前端 `ColumnsBar` 用 Popover 下拉 + 三态点击维护它。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / pydantic-ai 1.107 / openai SDK；React 19 + AntD 6 + Vite。pytest（后端），`npm run build`（前端，无单测）。

**测试运行约定：** 后端测试在 `E:/代码/GraphFlow/backend` 下 `python -m pytest`；前端在 `E:/代码/GraphFlow/frontend` 下 `npm run build`。提交前先 `git -C "E:/代码/GraphFlow" status` 确认工作树完整（中文路径下工具偶发漂移）。每个 `git add` 按文件精确添加，**绝不** add `项目设计.txt` / `.idea/` / `.codegraph/`。提交均带第二个 `-m`：`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

---

## File Structure

**新建：**
- `backend/app/thinking.py` — 纯函数 `thinking_extra_body(params) -> dict | None`，思考配置的唯一真源。
- `backend/tests/test_thinking.py` — 上述函数单测。

**修改：**
- `backend/app/services/llm.py` — `chat()` 注入 `extra_body`（节点路径）。
- `backend/app/agent/factory.py` — `create_model()` 注入 `extra_body`（Agent 路径）。
- `backend/app/engine/columns.py` — `_node_output` 改为薄包装，统一减 `drop_columns`。
- `backend/app/engine/runner.py` — `_write_unit` 加 `drop` 参数 + 四处调用点传 `drop_columns`。
- `frontend/src/canvas/forms/NodeConfigForm.tsx` — `ColumnsBar` 重做（Popover 下拉 + 三态）、`liveOutput` 减 drop、`LlmSynthForm`/`QcForm` 加思考控件、默认组件接线三态 `cycle`。

**修改（测试）：**
- `backend/tests/test_llm.py` — 加思考 extra_body 断言。
- `backend/tests/test_agent_factory.py` — 更新 `test_create_model_no_key`（思考默认开→settings 不再为 None）+ 加关闭用例。
- `backend/tests/test_columns.py` — 加 `drop_columns` 血缘减列用例。
- `backend/tests/test_runner.py` — 加端到端「llm 节点 drop 列、下游不含」用例。

任务顺序：先做思考模式（Task 1–3，独立、风险低），再做列删除（Task 4–5），最后前端（Task 6，一次性覆盖两块 UI）。

---

## Task 1: 思考配置纯函数 `thinking_extra_body`

**Files:**
- Create: `backend/app/thinking.py`
- Test: `backend/tests/test_thinking.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_thinking.py`:
```python
from app.thinking import thinking_extra_body


def test_default_enabled_high():
    assert thinking_extra_body({}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_disabled_returns_none():
    assert thinking_extra_body({"thinking_enabled": False}) is None


def test_custom_effort():
    assert thinking_extra_body({"reasoning_effort": "low"}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "low"}


def test_enabled_explicit_xhigh():
    assert thinking_extra_body({"thinking_enabled": True, "reasoning_effort": "xhigh"}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_thinking.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.thinking'`）

- [ ] **Step 3: Write minimal implementation**

`backend/app/thinking.py`:
```python
"""思考模式配置：把「是否开启思考 + 力度」翻译成 OpenAI 兼容请求的 extra_body。
默认开启、力度 high；关闭则整段不发（返回 None）。节点路径与 Agent 路径共用。"""


def thinking_extra_body(params: dict) -> dict | None:
    if not params.get("thinking_enabled", True):
        return None
    return {"thinking": {"type": "enabled"},
            "reasoning_effort": params.get("reasoning_effort", "high")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_thinking.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git -C "E:/代码/GraphFlow" add backend/app/thinking.py backend/tests/test_thinking.py
git -C "E:/代码/GraphFlow" commit -m "feat(thinking): 思考配置纯函数 thinking_extra_body（默认开启/high）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 节点路径注入 extra_body（`llm.chat`）

**Files:**
- Modify: `backend/app/services/llm.py`
- Test: `backend/tests/test_llm.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_llm.py` 末尾追加：
```python
async def test_thinking_default_extra_body(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u")
    assert fake.last_kwargs["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}


async def test_thinking_disabled_no_extra_body(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"thinking_enabled": False})
    assert "extra_body" not in fake.last_kwargs


async def test_thinking_custom_effort(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"reasoning_effort": "medium"})
    assert fake.last_kwargs["extra_body"]["reasoning_effort"] == "medium"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_llm.py::test_thinking_default_extra_body -v`
Expected: FAIL（`KeyError: 'extra_body'`）

- [ ] **Step 3: Write minimal implementation**

`backend/app/services/llm.py`，在文件顶部 import 区加：
```python
from app.thinking import thinking_extra_body
```

在 `chat()` 内，紧接现有 `if merged.get("json_mode"): ...` 块之后、`messages = []` 之前，插入：
```python
    eb = thinking_extra_body(merged)
    if eb is not None:
        kwargs["extra_body"] = eb
```

（参照原文件 35–38 行附近：）
```python
    if merged.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}
    eb = thinking_extra_body(merged)
    if eb is not None:
        kwargs["extra_body"] = eb
    messages = []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_llm.py -v`
Expected: PASS（含原有 8 个 + 新增 3 个；原有 `test_chat_success` 等不受影响，因为它们只断言特定键、不断言 extra_body 缺失）

- [ ] **Step 5: Commit**

```bash
git -C "E:/代码/GraphFlow" add backend/app/services/llm.py backend/tests/test_llm.py
git -C "E:/代码/GraphFlow" commit -m "feat(thinking): 节点路径 llm.chat 默认注入思考 extra_body" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Agent 路径注入 extra_body（`create_model`）

**Files:**
- Modify: `backend/app/agent/factory.py`
- Test: `backend/tests/test_agent_factory.py`

- [ ] **Step 1: Write the failing test**

修改 `backend/tests/test_agent_factory.py` 的 `test_create_model_no_key`（思考默认开启后 settings 不再为 None），并新增关闭用例。把原函数：
```python
def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    assert model.settings is None
```
替换为：
```python
def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    # 思考默认开启 → settings 带 extra_body，不再为 None
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_create_model_thinking_disabled():
    model = factory.create_model(_mc(default_params_json='{"thinking_enabled": false}'))
    assert "extra_body" not in (model.settings or {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_agent_factory.py::test_create_model_no_key -v`
Expected: FAIL（`TypeError: 'NoneType' object is not subscriptable`——当前 settings 为 None）

- [ ] **Step 3: Write minimal implementation**

`backend/app/agent/factory.py`，顶部 import 区加：
```python
from app.thinking import thinking_extra_body
```

把 `create_model` 改为：
```python
def create_model(mc: ModelConfig) -> OpenAIChatModel:
    params = json.loads(mc.default_params_json)
    kw = {k: params[k] for k in SETTINGS_KEYS if params.get(k) is not None}
    eb = thinking_extra_body(params)
    if eb is not None:
        kw["extra_body"] = eb
    provider = OpenAIProvider(
        base_url=mc.base_url,
        api_key=crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none")
    return OpenAIChatModel(mc.model_name, provider=provider,
                           settings=ModelSettings(**kw) if kw else None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_agent_factory.py -v`
Expected: PASS（含 `test_create_model_decrypts_key`——它只断言 temperature/max_tokens 在、json_mode 不在，新增的 extra_body 不影响这些断言）

- [ ] **Step 5: Commit**

```bash
git -C "E:/代码/GraphFlow" add backend/app/agent/factory.py backend/tests/test_agent_factory.py
git -C "E:/代码/GraphFlow" commit -m "feat(thinking): Agent 路径 create_model 默认注入思考 extra_body（覆盖全角色）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 列血缘减 drop_columns（`columns.py`）

**Files:**
- Modify: `backend/app/engine/columns.py`
- Test: `backend/tests/test_columns.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_columns.py` 末尾追加：
```python
def test_drop_columns_removed_from_output_and_downstream():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth",
          "config": {"output_column": "a", "drop_columns": ["secret"]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "secret"]})
    assert cols["ls"]["output"] == ["id", "q", "a"]   # secret 被本节点删除
    assert cols["out"]["input"] == ["id", "q", "a"]   # 下游看不到 secret


def test_drop_columns_empty_is_noop():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_column": "a"}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ls"]["output"] == ["q", "a"]          # 无 drop_columns 时行为不变
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_columns.py::test_drop_columns_removed_from_output_and_downstream -v`
Expected: FAIL（`assert ['id','q','secret','a'] == ['id','q','a']`）

- [ ] **Step 3: Write minimal implementation**

`backend/app/engine/columns.py`：把现有函数定义行
```python
def _node_output(node, input_cols: list[str], dataset_cols: dict[int, list[str]]) -> list[str]:
```
改名为：
```python
def _typed_output(node, input_cols: list[str], dataset_cols: dict[int, list[str]]) -> list[str]:
```
（函数体不动。）然后在其下方新增薄包装：
```python
def _node_output(node, input_cols: list[str], dataset_cols: dict[int, list[str]]) -> list[str]:
    out = _typed_output(node, input_cols, dataset_cols)
    drop = set(node.config.get("drop_columns") or [])
    return [c for c in out if c not in drop] if drop else out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_columns.py -v`
Expected: PASS（原有 ~16 个 + 新增 2 个全过）

- [ ] **Step 5: Commit**

```bash
git -C "E:/代码/GraphFlow" add backend/app/engine/columns.py backend/tests/test_columns.py
git -C "E:/代码/GraphFlow" commit -m "feat(columns): 列血缘统一减 drop_columns（删除列下游不可见）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 引擎落库剔列（`runner._write_unit`）

**Files:**
- Modify: `backend/app/engine/runner.py`
- Test: `backend/tests/test_runner.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_runner.py` 末尾追加（`DROP_GRAPH` 让 llm 节点删掉输入列 `q`）：
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_runner.py::test_drop_columns_excluded_from_node_output -v`
Expected: FAIL（`assert 'q' not in {'q': '问0', 'a': '答[...]'}`）

- [ ] **Step 3: Write minimal implementation**

`backend/app/engine/runner.py`：

(a) 给 `_write_unit` 加 `drop` 参数并在序列化前剔列。把签名与序列化行改为：
```python
async def _write_unit(session_factory, run_id, node_id, row_idx, status, out_rows, error,
                      usage: dict | None = None, qc_round: int = 0, drop=None):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.row_idx == row_idx
        ))).scalar_one_or_none()
        if rec is None:
            rec = RunRow(run_id=run_id, node_id=node_id, row_idx=row_idx, attempt=0)
            s.add(rec)
        rec.status = status
        if drop:
            drop_set = set(drop)
            out_rows = [{k: v for k, v in r.items() if k not in drop_set} for r in out_rows]
        rec.data_json = json.dumps(out_rows, ensure_ascii=False)
        rec.error = error
        rec.attempt = (rec.attempt or 0) + 1
        rec.qc_round = qc_round
        if usage:
            rec.prompt_tokens = usage["prompt_tokens"]
            rec.completion_tokens = usage["completion_tokens"]
        await s.commit()
```

(b) 四处「done」写入点传 `drop`：

- `_run_llm_node` 的 `work()` 内成功分支：
```python
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "",
                                  usage=usage, drop=cfg.get("drop_columns"))
```
- `_run_http_node` 的 `work()` 内成功分支：
```python
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "",
                                  usage=usage, drop=cfg.get("drop_columns"))
```
- `_run_qc_node` 末尾最终写通过行：
```python
    await _write_unit(session_factory, run_id, node.id, 0, "done", passed, "",
                      usage=usage, qc_round=rounds, drop=cfg.get("drop_columns"))
```
- `_run_barrier_node` 的成功写入（覆盖 auto_process/output/input）：
```python
    await _write_unit(session_factory, run_id, node.id, 0, "done", out, "",
                      drop=node.config.get("drop_columns"))
```

（失败分支写 `[]`，drop 对空列表无副作用，无需改动。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_runner.py -v`
Expected: PASS（新增 1 个 + 原有全过；`drop_columns` 默认缺省=None→不剔列，既有用例行为不变）

- [ ] **Step 5: Run full backend suite (回归)**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q`
Expected: PASS（全绿；预期 348 + 本批新增约 12 个）

- [ ] **Step 6: Commit**

```bash
git -C "E:/代码/GraphFlow" add backend/app/engine/runner.py backend/tests/test_runner.py
git -C "E:/代码/GraphFlow" commit -m "feat(engine): _write_unit 统一切口按 drop_columns 落库剔列（四类节点）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 前端——列下拉框 + 三态删除 + 思考控件

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`

> 前端无单测，验证靠 `npm run build` 通过 + 代码审查逻辑。

- [ ] **Step 1: import 加 Popover**

把第 2 行的 antd import 改为（加 `Popover`）：
```tsx
import { Button, Input, InputNumber, Popover, Radio, Select, Space, Switch, Table, Tag } from 'antd'
```

- [ ] **Step 2: `liveOutput` 末尾统一减 drop_columns**

把 `liveOutput` 函数整体替换为：
```tsx
function liveOutput(type: string, config: Record<string, any>, inputCols: string[]): string[] {
  const drop = new Set<string>(config.drop_columns ?? [])
  const sub = (cols: string[]) => cols.filter((c) => !drop.has(c))
  if (type === 'llm_synth') {
    if ((config.output_mode ?? 'column') === 'json') return sub(uniq([...inputCols, ...(config.output_columns ?? [])]))
    return sub(uniq([...inputCols, config.output_column || 'output']))
  }
  if (type === 'auto_process') {
    let cols = [...inputCols]
    for (const op of config.operations ?? []) {
      if (op.op === 'rename') { const map = op.mapping ?? {}; cols = cols.map((c) => map[c] ?? c) }
      else if (op.op === 'drop') { const d = new Set(op.columns ?? []); cols = cols.filter((c) => !d.has(c)) }
      else if (op.op === 'concat') { if (op.target && !cols.includes(op.target)) cols = [...cols, op.target] }
      else if (op.op === 'agent') { cols = op.output_columns?.length ? uniq(op.output_columns) : cols }
    }
    return sub(cols)
  }
  if (type === 'http_fetch') return sub(uniq([...inputCols, ...Object.keys(config.extract ?? {})]))
  return sub(inputCols)   // qc / output 透传 - drop
}
```

- [ ] **Step 3: `ColumnsBar` 重做（Popover 下拉 + 三态摘要）**

把整个 `ColumnsBar` 函数替换为：
```tsx
function ColumnsBar({ inputCols, outputCols, referenced = [], dropped = [], onCycle }: {
  inputCols: string[]; outputCols: string[]; referenced?: string[]; dropped?: string[]
  onCycle?: (col: string) => void
}) {
  const refSet = new Set(referenced)
  const dropSet = new Set(dropped)
  const produced = outputCols.filter((c) => !inputCols.includes(c))
  const fed = inputCols.filter((c) => refSet.has(c) && !dropSet.has(c))
  const del = inputCols.filter((c) => dropSet.has(c))
  const colorOf = (c: string) => (dropSet.has(c) ? 'red' : refSet.has(c) ? 'green' : undefined)
  const inputList = (
    <div style={{ maxHeight: 280, overflowY: 'auto', maxWidth: 320 }}>
      {inputCols.length === 0
        ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
        : inputCols.map((c) => (
          <Tag key={c} color={colorOf(c)}
               style={{ cursor: onCycle ? 'pointer' : 'default', marginBottom: 6 }}
               onClick={() => onCycle?.(c)}>{c}</Tag>))}
    </div>
  )
  const outputList = (
    <div style={{ maxHeight: 280, overflowY: 'auto', maxWidth: 320 }}>
      {outputCols.length === 0
        ? <span style={{ color: '#bbb' }}>（无）</span>
        : outputCols.map((c) => (
          <Tag key={c} color={produced.includes(c) ? 'blue' : undefined}
               style={{ marginBottom: 6 }}>{c}</Tag>))}
    </div>
  )
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
        <Popover trigger="click" placement="bottomLeft" content={inputList}
                 title={onCycle ? '点击列名循环：透传→喂模型→删除' : '全部输入列'}>
          <Button size="small">输入列 ({inputCols.length}) ▾</Button>
        </Popover>
        {fed.map((c) => <Tag key={c} color="green" style={{ margin: 0 }}>{c}</Tag>)}
        {del.map((c) => <Tag key={c} color="red" style={{ margin: 0 }}>{c}</Tag>)}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
        <Popover trigger="click" placement="bottomLeft" content={outputList} title="全部输出列">
          <Button size="small">输出列 ({outputCols.length}) ▾</Button>
        </Popover>
        {produced.map((c) => <Tag key={c} color="blue" style={{ margin: 0 }}>{c}</Tag>)}
      </div>
      {onCycle && <div style={{ color: '#999', marginTop: 6 }}>
        <span style={{ color: '#52c41a' }}>绿</span>=喂给模型；
        <span style={{ color: '#cf1322' }}>红</span>=删除(下游不可见)；
        <span style={{ color: '#1677ff' }}>蓝</span>=本节点新增；灰=透传保存。
      </div>}
    </div>
  )
}
```

- [ ] **Step 4: 默认组件接线三态 cycle + outputCols 统一走 liveOutput**

在 `NodeConfigForm` 默认导出组件里，把这段：
```tsx
  const outputCols = type === 'llm_synth' || type === 'auto_process' || type === 'http_fetch'
    ? liveOutput(type, config, inputCols) : nodeCols.output
  ...
  const insertField = type === 'http_fetch' ? 'url' : 'user_prompt'
  const canInsert = type === 'llm_synth' || type === 'qc' || type === 'http_fetch'
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols} referenced={referenced}
                onToggle={canInsert
                  ? (c) => onChange({ ...config, [insertField]: toggleColRef(config[insertField] ?? '', c) })
                  : undefined} />
  )
```
替换为：
```tsx
  const outputCols = type === 'input' ? nodeCols.output : liveOutput(type, config, inputCols)
  ...
  const insertField = type === 'http_fetch' ? 'url' : 'user_prompt'
  const canInsert = type === 'llm_synth' || type === 'qc' || type === 'http_fetch'
  const dropped: string[] = config.drop_columns ?? []
  // 三态循环：灰(透传)→绿(喂模型,插{{列}})→红(删除,移{{列}}并入 drop_columns)→灰
  const cycle = (col: string) => {
    if (dropped.includes(col)) {
      onChange({ ...config, drop_columns: dropped.filter((c) => c !== col) })
    } else if (referenced.includes(col)) {
      onChange({ ...config, [insertField]: toggleColRef(config[insertField] ?? '', col),
                 drop_columns: [...dropped, col] })
    } else {
      onChange({ ...config, [insertField]: toggleColRef(config[insertField] ?? '', col) })
    }
  }
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols} referenced={referenced}
                dropped={dropped} onCycle={canInsert ? cycle : undefined} />
  )
```
（注意：原 `referenced` 行保持不变，仍在此段之前。）

- [ ] **Step 5: `LlmSynthForm` 思考控件**

在 `LlmSynthForm` 的第二个 `<Space wrap>`（temperature/top_p/max_tokens/超时/JSON 模式）里，把 `JSON 模式` 那个 `<Field>` 之后、`</Space>` 之前插入：
```tsx
        <Field label="开启思考"><Switch checked={params.thinking_enabled ?? true}
          onChange={(v) => patchParams({ thinking_enabled: v })} /></Field>
        <Field label="思考力度"><Select style={{ width: 100 }}
          value={params.reasoning_effort ?? 'high'} disabled={!(params.thinking_enabled ?? true)}
          onChange={(v) => patchParams({ reasoning_effort: v })}
          options={['low', 'medium', 'high', 'xhigh'].map((e) => ({ value: e, label: e }))} /></Field>
```

- [ ] **Step 6: `QcForm` 思考控件**

在 `QcForm` 的 `<Space wrap>`（temperature/top_p/max_tokens/超时）里，把 `超时(秒)` 那个 `<Field>` 之后、`</Space>` 之前插入：
```tsx
        <Field label="开启思考"><Switch checked={params.thinking_enabled ?? true}
          onChange={(v) => patchParams({ thinking_enabled: v })} /></Field>
        <Field label="思考力度"><Select style={{ width: 100 }}
          value={params.reasoning_effort ?? 'high'} disabled={!(params.thinking_enabled ?? true)}
          onChange={(v) => patchParams({ reasoning_effort: v })}
          options={['low', 'medium', 'high', 'xhigh'].map((e) => ({ value: e, label: e }))} /></Field>
```

- [ ] **Step 7: 构建验证**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build`
Expected: 构建成功（无 TS 报错；产物输出到 `../backend/static/`）

- [ ] **Step 8: Commit**

```bash
git -C "E:/代码/GraphFlow" add frontend/src/canvas/forms/NodeConfigForm.tsx
git -C "E:/代码/GraphFlow" commit -m "feat(ui): 列 I/O 下拉框+三态删除(drop_columns) + llm/qc 思考控件" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾（全部任务完成后）

- [ ] 全后端套件回归：`cd "E:/代码/GraphFlow/backend" && python -m pytest -q` → 全绿。
- [ ] 前端构建：`cd "E:/代码/GraphFlow/frontend" && npm run build` → 成功。
- [ ] `git -C "E:/代码/GraphFlow" status` 确认工作树仅余禁提文件（项目设计.txt / .idea/ / .codegraph/）。
- [ ] 用 `superpowers:finishing-a-development-branch` 收尾：ff-merge 进 master、删分支、写记忆。
