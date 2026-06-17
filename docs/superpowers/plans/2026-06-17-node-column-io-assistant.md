# 节点列 I/O 清晰化 + 助手保留上下文 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 RedLotus 助手不保留上下文的 bug，并重做节点列 I/O 可视化（输入列点击切换绿底=喂给LLM、输出列透传/新增分色、取消下拉框），让“哪些列输入/输出、未处理列仍会全量保存”一眼可见。

**Architecture:** 后端给 `codegen`/`node-assist` 端点与生成函数加“当前配置/代码”入参并拼进模型提示（增量修改、保留已有）；前端把当前 config/code 回传，并重写 `ColumnsBar`（绿底点击切换 `{{列}}`、输出区透传/新增分色）。列血缘与引擎透传逻辑已正确，本批补一个透传回归测试并把透传列在 UI 显式标出。

**Tech Stack:** FastAPI + SQLAlchemy 2 async（SQLite，无 migration）、pydantic-ai、React 19 + AntD 6 + Vite。

**贯穿约束（KISS 硬规则）：** 最简实现、不预防未发生的 bug；提示词只含列名不含行值；签名加可选参数保持向后兼容；不加表不加列。

**测试运行环境：** 后端在 `backend/` 目录执行 `python -m pytest ...`；前端在 `frontend/` 目录执行 `npm run build`。

---

### Task 1: 后端 — 生成函数接收当前配置/代码 + 提示词增量指令

**Files:**
- Modify: `backend/app/agent/codegen.py`（`generate_code`、`generate_node_config`）
- Modify: `backend/app/agent/prompts/codegen_system.md`
- Modify: `backend/app/agent/prompts/node_assist_llm_synth.md`
- Modify: `backend/app/agent/prompts/node_assist_qc.md`
- Test: `backend/tests/test_assistant_context.py`（新建）

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_assistant_context.py`：

```python
import json

from app.agent import codegen


class _StubResult:
    def __init__(self, output):
        self.output = output


class _StubAgent:
    def __init__(self, captured, output):
        self._captured = captured
        self._output = output

    async def run(self, prompt):
        self._captured["prompt"] = prompt
        return _StubResult(self._output)


def _patch_agent(monkeypatch, captured, output):
    monkeypatch.setattr(codegen, "create_agent",
                        lambda *a, **k: _StubAgent(captured, output))


async def test_generate_code_includes_current_code(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"code": "def process(rows): return rows", "output_columns": ["x"]}')
    await codegen.generate_code(None, "把B列转成B2", ["A", "B"],
                                current_code="def process(rows):\n    # A->A1\n    return rows")
    assert "A->A1" in cap["prompt"]              # 现有代码进了模型提示
    assert "把B列转成B2" in cap["prompt"]        # 新指令也在


async def test_generate_code_empty_current_code_degrades(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"code": "x", "output_columns": []}')
    await codegen.generate_code(None, "做点啥", ["A"], current_code="")
    assert "现有代码" not in cap["prompt"]        # 没有现有代码时不加该段（退化为现状）


async def test_generate_node_config_includes_current_config(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"system_prompt": "s", "user_prompt": "u", "output_mode": "column", "output_column": "B2"}')
    await codegen.generate_node_config(None, "llm_synth", "把B列转成B2", ["A", "B"],
                                       current_config={"user_prompt": "把 {{A}} 翻译成 A1"})
    assert "把 {{A}} 翻译成 A1" in cap["prompt"]  # 现有配置进了模型提示


async def test_generate_node_config_no_current_degrades(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"system_prompt": "s", "user_prompt": "u"}')
    await codegen.generate_node_config(None, "qc", "判断是否切题", ["q", "a"], current_config=None)
    assert "现有节点配置" not in cap["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run（在 `backend/`）: `python -m pytest tests/test_assistant_context.py -v`
Expected: FAIL — `generate_code()`/`generate_node_config()` 不接受 `current_code`/`current_config` 关键字参数（TypeError）。

- [ ] **Step 3: Write minimal implementation**

`backend/app/agent/codegen.py`，把 `generate_code` 与 `generate_node_config` 改为：

```python
async def generate_code(model, instruction: str, columns: list[str], current_code: str = "") -> dict:
    """按指令+上游列名生成 {code, output_columns}；不执行、不预览。
    传入 current_code 时要求模型在其基础上增量修改、保留已有处理。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    prompt = _user_prompt(instruction, columns)
    if current_code.strip():
        prompt += ("\n\n现有代码（请在此基础上增量修改，保留已有处理逻辑，"
                   "不要丢失之前的转换）：\n" + current_code)
    result = await agent.run(prompt)
    data = json.loads(strip_code_fences(str(result.output or "")))
    return {"code": data.get("code", ""), "output_columns": data.get("output_columns", [])}
```

```python
async def generate_node_config(model, node_type: str, instruction: str, columns: list[str],
                               current_config: dict | None = None) -> dict:
    """临时单 Agent 为指定节点产出配置 JSON（不跑代码，仅生成提示词）。未知 node_type 抛 KeyError。
    传入 current_config 时要求模型在其基础上增量修改、保留已有提示词与需求。"""
    agent = create_agent(model, [], NODE_ASSIST_INSTRUCTIONS[node_type])
    prompt = _user_prompt(instruction, columns)
    if current_config:
        prompt += ("\n\n现有节点配置（请在此基础上增量修改，保留已有提示词中的处理，"
                   "不要丢失之前的需求）：\n" + json.dumps(current_config, ensure_ascii=False))
    result = await agent.run(prompt)
    return json.loads(strip_code_fences(str(result.output or "")))
```

然后在三个提示词文件末尾各加一行增量指令。

`backend/app/agent/prompts/codegen_system.md` 末尾追加：

```
若用户额外提供了「现有代码」，必须在其基础上增量修改：保留已有的处理逻辑，把新指令的处理叠加进去，绝不丢弃之前的转换。
```

`backend/app/agent/prompts/node_assist_llm_synth.md` 末尾追加：

```
- 若用户额外提供了「现有节点配置」，必须在其基础上增量修改：保留已有提示词中的处理，把新指令叠加进去，绝不丢弃之前的需求。
```

`backend/app/agent/prompts/node_assist_qc.md` 末尾追加：

```
- 若用户额外提供了「现有节点配置」，必须在其基础上增量修改：保留已有判定规则，把新指令叠加进去，绝不丢弃之前的需求。
```

- [ ] **Step 4: Run test to verify it passes**

Run（在 `backend/`）: `python -m pytest tests/test_assistant_context.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/codegen.py backend/app/agent/prompts/codegen_system.md backend/app/agent/prompts/node_assist_llm_synth.md backend/app/agent/prompts/node_assist_qc.md backend/tests/test_assistant_context.py
git commit -m "feat(agent): codegen/node-assist 生成函数接收当前代码/配置，提示词增量修改保留已有" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 后端 — 端点透传 current_code / current_config

**Files:**
- Modify: `backend/app/routers/agent.py`（`CodegenIn`、`codegen`、`NodeAssistIn`、`node_assist`）
- Test: `backend/tests/test_assistant_context.py`（追加）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_assistant_context.py` 末尾追加（端点集成测试，用 conftest 的 `auth_client`）：

```python
async def _make_model_and_wf(auth_client, node_id, node_type):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": {
        "nodes": [{"id": node_id, "type": node_type, "config": {}}], "edges": []}})
    return mc, wf


async def test_codegen_endpoint_passes_current_code(auth_client, monkeypatch):
    cap = {}

    async def fake_gen(model, instruction, columns, current_code=""):
        cap["current_code"] = current_code
        return {"code": "x", "output_columns": []}

    monkeypatch.setattr("app.routers.agent.generate_code", fake_gen)
    mc, wf = await _make_model_and_wf(auth_client, "ap", "auto_process")
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wf["id"], "node_id": "ap", "instruction": "做点啥",
        "model_config_id": mc["id"], "current_code": "PRIOR_CODE"})
    assert r.status_code == 200
    assert cap["current_code"] == "PRIOR_CODE"


async def test_node_assist_endpoint_passes_current_config(auth_client, monkeypatch):
    cap = {}

    async def fake_cfg(model, node_type, instruction, columns, current_config=None):
        cap["current_config"] = current_config
        return {"system_prompt": "s", "user_prompt": "u"}

    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": mc["id"],
        "current_config": {"user_prompt": "把 {{A}} 翻译成 A1"}})
    assert r.status_code == 200
    assert cap["current_config"] == {"user_prompt": "把 {{A}} 翻译成 A1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run（在 `backend/`）: `python -m pytest tests/test_assistant_context.py -v -k endpoint`
Expected: FAIL — 端点不接受 `current_code`/`current_config` 字段（pydantic 忽略多余字段 → `cap` 拿到默认 `""`/`None`，断言不等）。

- [ ] **Step 3: Write minimal implementation**

`backend/app/routers/agent.py`：

`CodegenIn` 改为：

```python
class CodegenIn(BaseModel):
    workflow_id: int
    node_id: str
    instruction: str
    model_config_id: int
    current_code: str | None = None
```

`codegen` 端点里的生成调用改为：

```python
    result = await generate_code(mc, body.instruction, columns, current_code=body.current_code or "")
```

`NodeAssistIn` 改为：

```python
class NodeAssistIn(BaseModel):
    workflow_id: int
    node_id: str
    node_type: str
    instruction: str
    model_config_id: int
    current_config: dict | None = None
```

`node_assist` 端点里的生成调用改为：

```python
    config = await codegen_mod.generate_node_config(
        mc, body.node_type, body.instruction, columns, current_config=body.current_config)
```

- [ ] **Step 4: Run test to verify it passes**

Run（在 `backend/`）: `python -m pytest tests/test_assistant_context.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/agent.py backend/tests/test_assistant_context.py
git commit -m "feat(agent): codegen/node-assist 端点透传 current_code/current_config" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 前端 — 助手回传当前 config / code

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`NodeAssist`、其两处调用、`AgentOpFields`）

- [ ] **Step 1: 让 NodeAssist 接收并回传当前 config**

把 `NodeAssist` 组件签名（当前为 `{ nodeType, workflowId, nodeId, onApply }`）改为也接收 `config`：

```tsx
function NodeAssist({ nodeType, workflowId, nodeId, config, onApply }: {
  nodeType: string; workflowId?: number; nodeId?: string
  config: Record<string, any>
  onApply: (config: Record<string, any>) => void
}) {
```

把其中 `api.post<NodeAssistOut>('/api/agent/node-assist', {...})` 的请求体改为带当前配置：

```tsx
      const r = await api.post<NodeAssistOut>('/api/agent/node-assist', {
        workflow_id: workflowId, node_id: nodeId, node_type: nodeType,
        instruction, model_config_id: modelSel, current_config: config,
      })
```

- [ ] **Step 2: 两处调用 NodeAssist 处传入 config**

`LlmSynthForm` 里：

```tsx
      <NodeAssist nodeType="llm_synth" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
```

`QcForm` 里：

```tsx
      <NodeAssist nodeType="qc" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
```

- [ ] **Step 3: AgentOpFields 回传当前代码**

`AgentOpFields` 的 `generate` 里，把 `api.post<CodegenOut>('/api/agent/codegen', {...})` 的请求体改为带当前代码：

```tsx
      const r = await api.post<CodegenOut>('/api/agent/codegen', {
        workflow_id: workflowId, node_id: nodeId,
        instruction: op.instruction, model_config_id: modelSel, current_code: op.code,
      })
```

- [ ] **Step 4: 验证构建通过**

Run（在 `frontend/`）: `npm run build`
Expected: tsc + vite 构建无错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(web): 助手回传当前节点 config/code，支持增量修改保留上下文" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 前端 — ColumnsBar 重做（绿底点击切换 + 输出透传/新增分色 + 取消下拉框）

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`toggleColRef` 助手、`ColumnsBar`、默认导出的接线；移除 `MANY_COLS`）

- [ ] **Step 1: 加 toggleColRef 助手，移除 MANY_COLS**

在 `referencedCols`（已存在）之后、把 `const MANY_COLS = 12` 这一行**替换**为 `toggleColRef`：

```tsx
// 切换某列在文本里的 {{列}} 引用：已存在则删除全部该列占位，否则在末尾追加
function toggleColRef(text: string, col: string): string {
  const esc = col.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp('\\{\\{\\s*' + esc + '\\s*\\}\\}', 'g')
  if (re.test(text ?? '')) return (text ?? '').replace(new RegExp('\\{\\{\\s*' + esc + '\\s*\\}\\}', 'g'), '')
  return (text ?? '') + `{{${col}}}`
}
```

- [ ] **Step 2: 重写 ColumnsBar（全量 chip、输入绿底点击切换、输出透传/新增分色）**

把整个 `ColumnsBar`（当前含 `many`/`Select` 分支的版本）替换为：

```tsx
function ColumnsBar({ inputCols, outputCols, referenced = [], onToggle }: {
  inputCols: string[]; outputCols: string[]; referenced?: string[]
  onToggle?: (col: string) => void
}) {
  const refSet = new Set(referenced)
  const produced = outputCols.filter((c) => !inputCols.includes(c))
  const passthrough = outputCols.filter((c) => inputCols.includes(c))
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ color: '#666', marginBottom: 6 }}>
        <span style={{ marginInlineEnd: 4 }}>输入列{onToggle ? '（点击切换是否喂给本节点）' : ''}：</span>
        {inputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
          : inputCols.map((c) => (
            <Tag key={c} color={refSet.has(c) ? 'green' : undefined}
                 style={{ cursor: onToggle ? 'pointer' : 'default', marginInlineEnd: 4, marginBottom: 4 }}
                 onClick={() => onToggle?.(c)}>{c}</Tag>))}
      </div>
      <div style={{ color: '#666' }}>
        <span style={{ marginInlineEnd: 4 }}>输出列：</span>
        {outputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无）</span>
          : <>
              {passthrough.map((c) => <Tag key={c} style={{ marginInlineEnd: 4, marginBottom: 4 }}>{c}</Tag>)}
              {produced.map((c) => <Tag key={c} color="blue" style={{ marginInlineEnd: 4, marginBottom: 4 }}>{c}</Tag>)}
            </>}
      </div>
      {onToggle && <div style={{ color: '#999', marginTop: 6 }}>
        <span style={{ color: '#52c41a' }}>绿色</span>=喂给本节点（已插入 {'{{列}}'}）；
        其余输入列不喂给模型，但仍会<b>透传保存</b>。输出列：灰=透传，<span style={{ color: '#1677ff' }}>蓝</span>=本节点新增。
      </div>}
    </div>
  )
}
```

- [ ] **Step 3: 默认导出接线改用 onToggle**

把默认导出 `NodeConfigForm` 里 `bar` 的定义（当前传 `onInsert`）替换为传 `onToggle`：

```tsx
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols} referenced={referenced}
                onToggle={canInsert
                  ? (c) => onChange({ ...config, [insertField]: toggleColRef(config[insertField] ?? '', c) })
                  : undefined} />
  )
```

（`referenced`、`insertField`、`canInsert`、`inputCols`、`outputCols` 沿用已有定义，无需改动。）

- [ ] **Step 4: 验证构建通过**

Run（在 `frontend/`）: `npm run build`
Expected: tsc + vite 构建无错误（`MANY_COLS` 已不再被引用；若残留引用，tsc 会报未定义——删除残留处）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(web): ColumnsBar 重做——输入列绿底点击切换{{列}}/输出列透传灰·新增蓝/取消下拉框" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 后端 — 全量透传回归测试

**Files:**
- Modify: `backend/tests/test_runner.py`（追加测试，复用其 imports 与 `make_run`/`run_it`/`patch_chat`）

- [ ] **Step 1: Write the failing test（先确认现状是否已透传）**

在 `backend/tests/test_runner.py` 末尾追加：

```python
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
```

- [ ] **Step 2: Run test to verify (预期直接通过——引擎已透传)**

Run（在 `backend/`）: `python -m pytest tests/test_runner.py::test_llm_passes_through_all_columns -v`
Expected: PASS。`run_llm_synth_row` 返回 `{**base, output_column: text}`，base 含全部 10 列，故输出节点每行含 10 列 + ans。
若意外 FAIL：说明存在隐藏的减列路径，按 systematic-debugging 定位根因（看 `_node_outputs`/`_barrier_output`/`run_llm_synth_row`）后按 KISS 修，使本测试通过。

- [ ] **Step 3: （仅当 Step 2 失败才需要）修复减列根因**

仅在 Step 2 失败时进行：定位并最小化修复使 base 全量透传。若 Step 2 已通过，跳过本步。

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_runner.py
git commit -m "test(engine): LLM 节点全量透传回归——10列入只处理1列，最终保存仍含全部10列" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾验证（全部任务完成后）

- [ ] 后端全套：在 `backend/` 跑 `python -m pytest -q`，应全绿（既有 + 本批新增 ~7 个用例）。
- [ ] 前端：在 `frontend/` 跑 `npm run build` 通过。
- [ ] 用 finishing-a-development-branch 收尾：测试全绿 → ff-merge 到 master → 删除 `feat/node-column-io-assistant` 分支（仅留 master）。

## 自查（plan 对 spec 覆盖核对）

- ✓ Part 1 助手保留上下文：Task 1（生成函数+提示词）+ Task 2（端点）+ Task 3（前端回传）。「A→A1 后 B→B2」由 current_code/current_config 进提示 + 提示词增量指令共同保证。
- ✓ Part 2 列 I/O 可视化：Task 4（绿底点击切换 `{{列}}`、输出透传灰/新增蓝、取消下拉框、图例）+ Task 3（助手改 config → 绿色自动同步，因 referenced 从 prompt 派生）。
- ✓ Part 3 保存全面：Task 5（10→1→全量透传回归测试）+ Task 4（输出区显式标出透传列让用户看见）。
- ✓ 绿底（非绿点）、绿=喂给LLM=prompt 含 `{{列}}`、未选列仍透传：Task 4。
- ✓ 方案 A（输出只读、产出列名在表单改、删列用 auto_process drop）：Task 4 输出区只读，未引入 per-node drop。
- ✓ 向后兼容：生成函数/端点新增参数均有默认值；既有 codegen/node-assist 调用不传也正常。
- ✓ 不加表不加列、无 migration。
