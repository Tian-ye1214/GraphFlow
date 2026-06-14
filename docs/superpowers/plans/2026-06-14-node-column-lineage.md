# 节点列血缘 + 质检可配参数 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个节点的输入/输出列在设计期明确可见（静态血缘 + 不可推断处自声明），并给质检节点开放 temperature/top_p 等参数。

**Architecture:** 新增纯函数 `engine/columns.py` 按 topo 顺序沿 normal 边推算每个节点的输入/输出列；input 取数据集列、llm_synth 加 output_column 或声明的 json `output_columns`、auto_process 按 op（rename/drop/concat/agent 声明列）变换、qc/output 透传。codegen 与新端点 `GET /workflows/{id}/columns` 复用它；前端面板展示输入/输出列、可点 chip 插入、缺列软提示。质检判定参数默认 temperature 0 但可被 `config.params` 覆盖。

**Tech Stack:** FastAPI + SQLAlchemy 2 async（SQLite）、pydantic-ai（FunctionModel 测试）、pytest（asyncio_mode=auto）；React 19 + antd 6 + TS（`npm run build` + `npx vitest run` 为前端门禁，无组件测试设施）。

**贯穿约束（KISS 硬规则）：** 最简实现、不预防未发生的 bug；api_key 全程加密绝不进响应/日志/提示词；所有模型/工作流/运行/数据集引用校验 `user_id`；不加表不加列。

**关键事实（实现者须知）：**
- `Workflow.graph_json` 默认 `'{"nodes": [], "edges": []}'`；`Dataset.columns_json` 默认 `"[]"`；`ModelConfig.default_params_json` 默认 `"{}"`；`ModelConfig.api_key_enc` 无默认（测试传 `api_key_enc=""`）。
- `app/engine/graph.py` 已有 `parse_graph(graph_json: str|dict) -> Graph`、`topo_order(g)`（只走 normal 边，有环抛 `GraphError`）、`upstream_ids(g, node_id)`（返回 normal 上游 source 列表）；`Node` 有 `.id/.type/.config`。
- 测试用 `monkeypatch.setattr(nodes.llm, "chat", fake)` 打桩 LLM；`run_qc_judge_row(config, row, mcs, pass_k, user_sem)`。
- 后端测试在 `E:/代码/GraphFlow/backend` 下跑：`python -m pytest ...`。

---

## Task 1: 列血缘纯函数 `engine/columns.py`

**Files:**
- Create: `backend/app/engine/columns.py`
- Test: `backend/tests/test_columns.py`

- [ ] **Step 1: Write the failing tests**

写 `backend/tests/test_columns.py`：

```python
from app.engine.columns import propagate_columns
from app.engine.graph import parse_graph


def _g(nodes, edges):
    return parse_graph({"nodes": nodes, "edges": edges})


def test_input_node_outputs_dataset_columns():
    g = _g([{"id": "in", "type": "input", "config": {"dataset_ids": [1]}}], [])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["in"]["output"] == ["id", "q", "category"]
    assert cols["in"]["input"] == []


def test_llm_synth_column_mode_adds_output_column():
    # 复刻 workflow 2：column 模式产出的是 output_column（a），而非 prompt 里写的 q_en
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_mode": "column", "output_column": "a"}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ls"]["output"] == ["id", "q", "category", "a"]


def test_llm_synth_column_mode_defaults_to_output():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ls"]["output"] == ["q", "output"]


def test_llm_synth_json_mode_uses_declared_output_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth",
          "config": {"output_mode": "json", "output_columns": ["q_en", "category_en"]}},
         {"id": "qc", "type": "qc", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "qc", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ls"]["output"] == ["id", "q", "category", "q_en", "category_en"]
    assert cols["qc"]["input"] == ["id", "q", "category", "q_en", "category_en"]


def test_auto_process_agent_op_adds_declared_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ap"]["output"] == ["q", "q_english"]


def test_auto_process_rename_drop_concat():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process", "config": {"operations": [
             {"op": "rename", "mapping": {"q": "question"}},
             {"op": "drop", "columns": ["category"]},
             {"op": "concat", "target": "merged", "columns": ["question"], "sep": "-"}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q", "category"]})
    assert cols["ap"]["output"] == ["question", "merged"]


def test_qc_passthrough_and_rescan_ignored():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_column": "a"}},
         {"id": "qc", "type": "qc", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "qc", "kind": "normal"},
         {"source": "qc", "target": "ls", "kind": "rescan"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["qc"]["output"] == ["q", "a"]  # rescan 反馈边不参与传播


def test_ordered_union_dedupes_across_upstreams():
    g = _g(
        [{"id": "a", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "b", "type": "input", "config": {"dataset_ids": [2]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "a", "target": "out", "kind": "normal"},
         {"source": "b", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"], 2: ["id", "x"]})
    assert cols["out"]["input"] == ["id", "q", "x"]


def test_empty_graph():
    assert propagate_columns(parse_graph({"nodes": [], "edges": []}), {}) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_columns.py -v`
Expected: FAIL（`ModuleNotFoundError: app.engine.columns`）

- [ ] **Step 3: Write `backend/app/engine/columns.py`**

```python
"""节点列血缘：按 topo 顺序静态推算每个节点的输入/输出列集合。"""
import json

from app.engine.graph import Graph, topo_order, upstream_ids
from app.models import Dataset


def _ordered_union(lists: list[list[str]]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for c in lst:
            if c not in out:
                out.append(c)
    return out


def _apply_op(cols: list[str], op: dict) -> list[str]:
    kind = op.get("op")
    if kind == "rename":
        mapping = op.get("mapping") or {}
        return [mapping.get(c, c) for c in cols]
    if kind == "drop":
        drop = set(op.get("columns") or [])
        return [c for c in cols if c not in drop]
    if kind == "concat":
        target = op.get("target")
        return cols + [target] if target and target not in cols else cols
    if kind == "agent":
        return _ordered_union([cols, op.get("output_columns") or []])
    return cols  # dedup/filter/cast/sample/shuffle 不改列集合


def _node_output(node, input_cols: list[str], dataset_cols: dict[int, list[str]]) -> list[str]:
    t = node.type
    if t == "input":
        return _ordered_union([dataset_cols.get(d, []) for d in node.config.get("dataset_ids", [])])
    if t == "llm_synth":
        if node.config.get("output_mode") == "json":
            return _ordered_union([input_cols, node.config.get("output_columns") or []])
        return _ordered_union([input_cols, [node.config.get("output_column") or "output"]])
    if t == "auto_process":
        cols = list(input_cols)
        for op in node.config.get("operations") or []:
            cols = _apply_op(cols, op)
        return cols
    return input_cols  # qc / output 透传


def propagate_columns(graph: Graph, dataset_cols: dict[int, list[str]]) -> dict[str, dict]:
    """返回 {node_id: {"input": [...], "output": [...]}}。只沿 normal 边、按 topo 顺序传播。"""
    inputs: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    for node in topo_order(graph):
        in_cols = _ordered_union([outputs.get(uid, []) for uid in upstream_ids(graph, node.id)])
        inputs[node.id] = in_cols
        outputs[node.id] = _node_output(node, in_cols, dataset_cols)
    return {n.id: {"input": inputs[n.id], "output": outputs[n.id]} for n in graph.nodes}


async def resolve_dataset_cols(s, graph: Graph, user_id: int) -> dict[int, list[str]]:
    """取图中所有 input 节点引用、且属于 user_id 的数据集列（租户隔离：非己有跳过）。"""
    ids = {d for n in graph.nodes if n.type == "input" for d in n.config.get("dataset_ids", [])}
    out: dict[int, list[str]] = {}
    for ds_id in ids:
        ds = await s.get(Dataset, ds_id)
        if ds is not None and ds.user_id == user_id:
            out[ds_id] = json.loads(ds.columns_json)
    return out
```

> 注：`resolve_dataset_cols` 的 DB 测试在 Task 2。本步把它一并写好（与纯函数同模块、便于 codegen 与 router 复用）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_columns.py -v`
Expected: PASS（9 个）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/columns.py backend/tests/test_columns.py
git commit -m "feat(columns): 节点列血缘静态传播纯函数" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 数据集列解析的租户隔离测试

**Files:**
- Test: `backend/tests/test_columns_resolve.py`
- （实现已在 Task 1 的 `resolve_dataset_cols` 内完成）

- [ ] **Step 1: Write the failing test**

写 `backend/tests/test_columns_resolve.py`：

```python
import json

from app.engine.columns import resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Dataset


async def test_resolve_dataset_cols_skips_foreign(client, session_factory):
    async with session_factory() as s:
        mine = Dataset(user_id=1, name="mine", columns_json=json.dumps(["q"]))
        theirs = Dataset(user_id=2, name="theirs", columns_json=json.dumps(["secret"]))
        s.add_all([mine, theirs])
        await s.commit()
        g = parse_graph({"nodes": [
            {"id": "a", "type": "input", "config": {"dataset_ids": [mine.id]}},
            {"id": "b", "type": "input", "config": {"dataset_ids": [theirs.id]}}], "edges": []})
        cols = await resolve_dataset_cols(s, g, user_id=1)
        mine_id = mine.id
    assert cols == {mine_id: ["q"]}  # 他人数据集被跳过，列名不泄露
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_columns_resolve.py -v`
Expected: PASS（`resolve_dataset_cols` 已在 Task 1 实现；本任务锁定租户隔离行为）

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_columns_resolve.py
git commit -m "test(columns): resolve_dataset_cols 租户隔离" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: codegen 改用静态列传播

**Files:**
- Modify: `backend/app/agent/codegen.py`（重写 `gather_upstream_columns`，删旧的 run/dataset 遍历）
- Modify: `backend/tests/test_agent_codegen.py`（删 last_run 测试、改 source 断言、加 llm 传播测试）
- Modify: `backend/tests/test_codegen_columns.py`（改 source 断言）

- [ ] **Step 1: Rewrite affected tests first（先改测试到期望行为）**

在 `backend/tests/test_agent_codegen.py`：
1. 顶部 import 改为 `from app.models import Dataset, DatasetRow, Workflow`（删除 `Run, RunRow, WorkflowVersion`）。
2. 把 `test_columns_from_dataset_fallback` 的断言改为：

```python
    assert source == "computed" and cols == ["q", "category"]
```

3. **删除** 整个 `test_columns_prefer_last_run`（静态传播不再有"优先上次运行"语义）。新增一条 llm 传播测试替代：

```python
async def test_columns_propagate_through_llm(client, session_factory):
    """静态传播能看到上游 llm_synth 声明的 json 输出列（如 q_en），无需任何历史运行。"""
    async with session_factory() as s:
        ds = Dataset(user_id=1, name="d", columns_json=json.dumps(["q"]))
        s.add(ds)
        await s.commit()
        graph = {"nodes": [
            {"id": "input_1", "type": "input", "config": {"dataset_ids": [ds.id]}},
            {"id": "llm_1", "type": "llm_synth",
             "config": {"output_mode": "json", "output_columns": ["q_en"]}},
            {"id": "auto_process_1", "type": "auto_process", "config": {}}],
            "edges": [{"source": "input_1", "target": "llm_1"},
                      {"source": "llm_1", "target": "auto_process_1"}]}
        wf = Workflow(user_id=1, name="w", graph_json=json.dumps(graph))
        s.add(wf)
        await s.commit()
        cols, source = await codegen.gather_upstream_columns(s, wf.id, "auto_process_1", user_id=1)
    assert source == "computed" and cols == ["q", "q_en"]
```

4. `test_columns_skip_foreign_dataset` 断言保持 `source == "none" and cols == []`（外来数据集被跳过 → input 节点无列 → 下游空）——无需改。
5. `test_columns_none_when_node_missing` 保持不变。

在 `backend/tests/test_codegen_columns.py`：把 `test_gather_upstream_columns_from_dataset` 末尾断言改为：

```python
    assert cols == ["q", "category"] and source == "computed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_codegen.py tests/test_codegen_columns.py -v`
Expected: FAIL（旧 `gather_upstream_columns` 仍返回 `"dataset"`/`"last_run"`，新断言不符）

- [ ] **Step 3: Rewrite `gather_upstream_columns` in `codegen.py`**

在 `backend/app/agent/codegen.py`：

1. import 区改为（删 `Graph, upstream_ids`、删 `Dataset, Run, RunRow, WorkflowVersion`，加 columns 模块）：

```python
import json

from app.agent.factory import create_agent
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Workflow
```

2. 把 `gather_upstream_columns` 整个替换为：

```python
async def gather_upstream_columns(s, workflow_id: int, node_id: str, user_id: int):
    """静态推算 node_id 的输入列（沿 llm/处理节点传播）。返回 (columns, source)。"""
    wf = await s.get(Workflow, workflow_id)
    if wf is None or wf.user_id != user_id:
        return [], "none"
    graph = parse_graph(wf.graph_json)
    if node_id not in {n.id for n in graph.nodes}:
        return [], "none"
    dataset_cols = await resolve_dataset_cols(s, graph, user_id)
    cols = propagate_columns(graph, dataset_cols)[node_id]["input"]
    return cols, ("computed" if cols else "none")
```

3. **删除** 这些不再使用的函数与常量：`_columns_of`、`_upstream_run_rows`、`_upstream_dataset_columns`、`SAMPLE_N`。（`strip_code_fences`、`_user_prompt`、`generate_code`、`generate_node_config`、`INSTRUCTIONS`、`NODE_ASSIST_INSTRUCTIONS` 全部保留。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_codegen.py tests/test_codegen_columns.py -v`
Expected: PASS（含新 `test_columns_propagate_through_llm`）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/codegen.py backend/tests/test_agent_codegen.py backend/tests/test_codegen_columns.py
git commit -m "feat(codegen): 上游列改用静态血缘传播（看得到 q_en）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `generate_code` 返回 {code, output_columns}

**Files:**
- Modify: `backend/app/agent/codegen.py`（`INSTRUCTIONS` 改 JSON 输出、`generate_code` 返回 dict）
- Modify: `backend/app/routers/agent.py`（codegen 端点返回 `output_columns`）
- Modify: `backend/tests/test_agent_codegen.py`（`generate_code` 两条测试改 JSON）
- Modify: `backend/tests/test_codegen_columns.py`（端点测试 fake 返回 dict）

- [ ] **Step 1: Rewrite affected tests first**

在 `backend/tests/test_agent_codegen.py`：
1. 把 `test_generate_code_returns_source_no_exec` 替换为：

```python
async def test_generate_code_returns_code_and_columns():
    """模型返回 JSON {code, output_columns}，generate_code 解析为 dict，不试跑、不预览。"""
    payload = json.dumps({"code": GOOD, "output_columns": ["ok"]})
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(payload)]))
    out = await codegen.generate_code(model, "加 ok 列", [])
    assert out == {"code": GOOD, "output_columns": ["ok"]}
```

2. 把 `test_generate_code_strips_fences_and_passes_columns` 替换为：

```python
async def test_generate_code_strips_fences_and_passes_columns():
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[-1].parts[-1].content
        payload = json.dumps({"code": GOOD, "output_columns": []})
        return ModelResponse(parts=[TextPart(f"```json\n{payload}\n```")])

    out = await codegen.generate_code(FunctionModel(fn), "去重", ["q", "category"])
    assert out["code"] == GOOD and out["output_columns"] == []
    # 上游列名进入 prompt，真实行值不进入
    assert "q" in seen["prompt"] and "category" in seen["prompt"]
```

3. 在 `test_instructions_guide_grouped_dedup` 末尾追加：

```python
    assert "output_columns" in INSTRUCTIONS  # 要求声明产出列
```

在 `backend/tests/test_codegen_columns.py`：把 `test_codegen_endpoint_returns_columns_no_preview` 的 fake 与断言改为：

```python
async def test_codegen_endpoint_returns_columns_no_preview(auth_client, session_factory, monkeypatch):
    async def fake_generate_code(model, instruction, columns):
        return {"code": "def process(rows):\n    return rows", "output_columns": []}
    monkeypatch.setattr("app.routers.agent.generate_code", fake_generate_code)
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        from app.models import ModelConfig
        mc = ModelConfig(user_id=uid, name="m", base_url="http://x", api_key_enc="")
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["a"]))
        s.add_all([mc, ds]); await s.flush()
        graph = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
                           {"id": "ap", "type": "auto_process", "config": {}}],
                 "edges": [{"source": "in", "target": "ap", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph))
        s.add(wf); await s.commit(); wf_id, mid = wf.id, mc.id
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wf_id, "node_id": "ap", "instruction": "删空行", "model_config_id": mid})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["a"] and body["output_columns"] == []
    assert "preview_rows" not in body and body["code"].startswith("def process")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_codegen.py tests/test_codegen_columns.py -v`
Expected: FAIL（`generate_code` 仍返回 str；端点未返回 `output_columns`）

- [ ] **Step 3: Rewrite `INSTRUCTIONS` + `generate_code` in `codegen.py`**

把 `INSTRUCTIONS` 常量整体替换为（保留 `def process(rows: list[dict]) -> list[dict]`/`pandas`/`groupby`/`分组`/`上游可用列` 等关键串）：

```python
INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
只输出一个 JSON 对象，不要任何解释或 markdown 围栏，形如：
{"code": "<Python 源码字符串>", "output_columns": ["<本次新增的列名>", ...]}
code 字段要求：
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas（可 import pandas as pd）；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。
output_columns 字段：列出 code 相对输入新增/产出的列名（仅新增的，没有则空数组 []）。

只给出上游可用列名（不含真实数据），请据指令与列名编写代码。
常见模式（按需选用、灵活组合，最后都 return 行字典列表，如 df.to_dict('records')）：
- 全局/多列复合去重：df.drop_duplicates(subset=[列...])（subset 含 'session' 即按 session 与其它列联合去重）。
- 分组内复杂处理（先按 session 分组、再对每组单独处理）：df.groupby('session', group_keys=False).apply(fn)。
- 过滤/改列：用 pandas 布尔索引或列表推导。"""
```

把 `generate_code` 替换为：

```python
async def generate_code(model, instruction: str, columns: list[str]) -> dict:
    """按指令+上游列名生成 {code, output_columns}；不执行、不预览。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, columns))
    data = json.loads(strip_code_fences(str(result.output or "")))
    return {"code": data.get("code", ""), "output_columns": data.get("output_columns", [])}
```

- [ ] **Step 4: Update codegen endpoint in `routers/agent.py`**

把 `codegen` 端点里这两行：

```python
    code = await generate_code(mc, body.instruction, columns)
    return {"code": code, "columns": columns, "sample_source": source}
```

替换为：

```python
    result = await generate_code(mc, body.instruction, columns)
    return {"code": result["code"], "output_columns": result["output_columns"],
            "columns": columns, "sample_source": source}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_codegen.py tests/test_codegen_columns.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent/codegen.py backend/app/routers/agent.py backend/tests/test_agent_codegen.py backend/tests/test_codegen_columns.py
git commit -m "feat(codegen): 生成代码同时声明产出列 output_columns" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: node-assist 支持 output_mode/output_columns（llm_synth）

**Files:**
- Modify: `backend/app/agent/codegen.py`（`NODE_ASSIST_INSTRUCTIONS["llm_synth"]`）
- Modify: `backend/tests/test_agent_codegen.py`（加 json 模式断言）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_agent_codegen.py` 加：

```python
async def test_generate_node_config_llm_synth_json_mode():
    out = json.dumps({"system_prompt": "你是翻译", "user_prompt": "翻译 {{q}} {{category}}",
                      "output_mode": "json", "output_columns": ["q_en", "category_en"]},
                     ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(out)]))
    cfg = await codegen.generate_node_config(model, "llm_synth", "把 q、category 翻译成英文拆两列", ["q", "category"])
    assert cfg["output_mode"] == "json" and cfg["output_columns"] == ["q_en", "category_en"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_codegen.py::test_generate_node_config_llm_synth_json_mode -v`
Expected: PASS 已经可能通过（`generate_node_config` 直接 `json.loads` 透传任意键）。若已 PASS，仍执行 Step 3 更新提示词以"引导 AI 主动产出 output_mode"，并保留此测试作回归锚。

> 说明：`generate_node_config` 无需改代码（它只 `json.loads(strip_code_fences(...))` 返回），本任务核心是把"何时用 json 模式 + 声明 output_columns"写进提示词，让真实模型会这么填。测试用 FunctionModel 固定返回，锁定契约。

- [ ] **Step 3: Update `NODE_ASSIST_INSTRUCTIONS["llm_synth"]` in `codegen.py`**

把 `NODE_ASSIST_INSTRUCTIONS` 里 `"llm_synth"` 一项替换为：

```python
    "llm_synth": """你为「LLM 合成」节点写配置：根据用户指令和上游可用列，写一段生成提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 指令只产出单列时：{"system_prompt":"...","user_prompt":"...","output_mode":"column","output_column":"<列名>"}。
- 指令产出多列时（让模型返回 JSON 再拆列）：{"system_prompt":"...","user_prompt":"...","output_mode":"json","output_columns":["<列名>",...]}，并让 user_prompt 要求模型只输出对应这些键的 JSON。
- user_prompt 用 {{列名}} 引用上游的可用列。""",
```

（`"qc"` 一项保持不变。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_codegen.py -v`
Expected: PASS（含原 `test_generate_node_config_llm_synth` 与新 json 模式测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/codegen.py backend/tests/test_agent_codegen.py
git commit -m "feat(node-assist): llm 助手按指令声明 output_mode 与 json 输出列" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 列查询端点 `GET /workflows/{id}/columns`

**Files:**
- Modify: `backend/app/routers/workflows.py`
- Test: `backend/tests/test_workflow_columns.py`

- [ ] **Step 1: Write the failing tests**

写 `backend/tests/test_workflow_columns.py`：

```python
import json

from sqlalchemy import select

from app.models import Dataset, User, Workflow


async def test_workflow_columns_endpoint(auth_client, session_factory):
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        ds = Dataset(user_id=uid, name="d", columns_json=json.dumps(["id", "q", "category"]))
        s.add(ds)
        await s.flush()
        graph = {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds.id]}},
            {"id": "ls", "type": "llm_synth", "config": {"output_mode": "json", "output_columns": ["q_en"]}},
            {"id": "qc", "type": "qc", "config": {}}],
            "edges": [{"source": "in", "target": "ls", "kind": "normal"},
                      {"source": "ls", "target": "qc", "kind": "normal"}]}
        wf = Workflow(user_id=uid, name="w", graph_json=json.dumps(graph))
        s.add(wf)
        await s.commit()
        wf_id = wf.id
    r = await auth_client.get(f"/api/workflows/{wf_id}/columns")
    assert r.status_code == 200
    body = r.json()
    assert body["ls"]["output"] == ["id", "q", "category", "q_en"]
    assert body["qc"]["input"] == ["id", "q", "category", "q_en"]


async def test_workflow_columns_404_foreign(auth_client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=999, name="other", graph_json=json.dumps({"nodes": [], "edges": []}))
        s.add(wf)
        await s.commit()
        wf_id = wf.id
    r = await auth_client.get(f"/api/workflows/{wf_id}/columns")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workflow_columns.py -v`
Expected: FAIL（端点不存在 → 404 for the first too, or route-not-found）

- [ ] **Step 3: Add endpoint to `workflows.py`**

在 import 区加：

```python
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import parse_graph
```

在 `get_workflow`（`@router.get("/{wf_id}")`）**之后**加新端点：

```python
@router.get("/{wf_id}/columns")
async def workflow_columns(wf_id: int, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    graph = parse_graph(wf.graph_json)
    dataset_cols = await resolve_dataset_cols(session, graph, user.id)
    return propagate_columns(graph, dataset_cols)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workflow_columns.py -v`
Expected: PASS（2 个）

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/workflows.py backend/tests/test_workflow_columns.py
git commit -m "feat(workflows): GET /columns 返回各节点输入/输出列" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 质检判定参数默认 0、可覆盖

**Files:**
- Modify: `backend/app/engine/nodes.py:170`（`run_qc_judge_row` 的 params 行）
- Test: `backend/tests/test_qc_params.py`

- [ ] **Step 1: Write the failing tests**

写 `backend/tests/test_qc_params.py`：

```python
import asyncio

from app.engine import nodes
from app.models import ModelConfig


async def test_qc_params_user_overrides_temperature(monkeypatch):
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"pass": true, "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    config = {"system_prompt": "判定", "user_prompt": "{{q}}",
              "params": {"temperature": 0.7, "top_p": 0.9}}
    await nodes.run_qc_judge_row(config, {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["temperature"] == 0.7
    assert seen["params"]["top_p"] == 0.9
    assert seen["params"]["json_mode"] is True


async def test_qc_params_default_temperature_zero(monkeypatch):
    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return '{"pass": true, "reason": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(nodes.llm, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="m", base_url="http://x", api_key_enc="")
    await nodes.run_qc_judge_row({"system_prompt": "判定", "user_prompt": "{{q}}"},
                                 {"q": "非空"}, [mc], 1, asyncio.Semaphore(4))
    assert seen["params"]["temperature"] == 0  # 未设时默认确定性
    assert seen["params"]["json_mode"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_qc_params.py -v`
Expected: FAIL（`test_qc_params_user_overrides_temperature` 失败——现有代码把 temperature 写死 0，覆盖了用户的 0.7）

- [ ] **Step 3: Change the params line in `nodes.py`**

把 `run_qc_judge_row` 里：

```python
    params = {**config.get("params", {}), "json_mode": True, "temperature": 0}
```

改为：

```python
    params = {"temperature": 0, **config.get("params", {}), "json_mode": True}
```

（空样本兜底与 system 锚定句不动。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_qc_params.py tests/test_qc_empty.py -v`
Expected: PASS（新 2 条；批次五 `test_qc_empty.py` 的 2 条仍绿——其 config 无 `params`，默认 temperature 仍为 0、锚定句仍在）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/nodes.py backend/tests/test_qc_params.py
git commit -m "feat(qc): 判定参数默认 temperature 0 但可被 config.params 覆盖" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 前端 — 类型 + 取列 + 列展示条（ColumnsBar）

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`

> 前端无组件测试设施；门禁 = `npm run build`（tsc -b && vite build）+ `npx vitest run`（既有 11 单测保绿）。在 `E:/代码/GraphFlow/frontend` 下执行。

- [ ] **Step 1: Update `api/types.ts`**

把 `CodegenOut`/`NodeAssistOut` 替换，并新增 `ColumnsMap`：

```typescript
export interface CodegenOut {
  code: string
  output_columns: string[]
  columns: string[]
  sample_source: 'computed' | 'none'
}

export interface NodeAssistOut {
  config: Record<string, any>
  sample_source: 'computed' | 'none'
}

export type ColumnsMap = Record<string, { input: string[]; output: string[] }>
```

- [ ] **Step 2: Add imports + helpers + ColumnsBar in `NodeConfigForm.tsx`**

1. 顶部 antd import 增加 `Tag`：

```typescript
import { Button, Input, InputNumber, Radio, Select, Space, Switch, Table, Tag } from 'antd'
```

2. types import 增加 `ColumnsMap`：

```typescript
import type { CodegenOut, ColumnsMap, Dataset, ModelConfig, NodeAssistOut, RowsPage } from '../../api/types'
```

3. 在 `Field` 组件之后插入以下纯函数与组件：

```typescript
const TPL_RE = /\{\{\s*([^{}]+?)\s*\}\}/g
function missingCols(text: string, inputCols: string[]): string[] {
  const out: string[] = []
  for (const m of (text ?? '').matchAll(TPL_RE)) {
    if (!inputCols.includes(m[1]) && !out.includes(m[1])) out.push(m[1])
  }
  return out
}

function MissingColsWarning({ text, inputCols }: { text: string; inputCols: string[] }) {
  const miss = missingCols(text, inputCols)
  if (miss.length === 0) return null
  return (
    <div style={{ color: '#d4380d', fontSize: 12, marginTop: 4 }}>
      ⚠ 引用了上游未产出的列：{miss.map((c) => `{{${c}}}`).join('、')}
    </div>
  )
}

const uniq = (arr: string[]) => arr.filter((c, i) => arr.indexOf(c) === i)

function liveOutput(type: string, config: Record<string, any>, inputCols: string[]): string[] {
  if (type === 'llm_synth') {
    if ((config.output_mode ?? 'column') === 'json') return uniq([...inputCols, ...(config.output_columns ?? [])])
    return uniq([...inputCols, config.output_column || 'output'])
  }
  if (type === 'auto_process') {
    let cols = [...inputCols]
    for (const op of config.operations ?? []) {
      if (op.op === 'rename') { const map = op.mapping ?? {}; cols = cols.map((c) => map[c] ?? c) }
      else if (op.op === 'drop') { const d = new Set(op.columns ?? []); cols = cols.filter((c) => !d.has(c)) }
      else if (op.op === 'concat') { if (op.target && !cols.includes(op.target)) cols = [...cols, op.target] }
      else if (op.op === 'agent') { cols = uniq([...cols, ...(op.output_columns ?? [])]) }
    }
    return cols
  }
  return inputCols
}

function ColumnsBar({ inputCols, outputCols, onInsert }: {
  inputCols: string[]; outputCols: string[]; onInsert?: (col: string) => void
}) {
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ color: '#666', marginBottom: 4 }}>
        输入列：{inputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
          : inputCols.map((c) => (
            <Tag key={c} style={{ cursor: onInsert ? 'pointer' : 'default', marginInlineEnd: 4 }}
                 onClick={() => onInsert?.(c)}>{c}</Tag>))}
      </div>
      <div style={{ color: '#666' }}>
        输出列：{outputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无）</span>
          : outputCols.map((c) => <Tag key={c} color="blue" style={{ marginInlineEnd: 4 }}>{c}</Tag>)}
      </div>
      {onInsert && <div style={{ color: '#999', marginTop: 4 }}>点输入列标签即可插入 {'{{列}}'} 到 User Prompt</div>}
    </div>
  )
}
```

- [ ] **Step 3: Wire fetch + bar into the default `NodeConfigForm`**

把文件末尾的 `export default function NodeConfigForm(...)` 整体替换为：

```typescript
export default function NodeConfigForm({ type, config, onChange, workflowId, nodeId }: FormProps & {
  type: string; workflowId?: number; nodeId?: string
}) {
  const [colsMap, setColsMap] = useState<ColumnsMap>({})
  useEffect(() => {
    if (workflowId) void api.get<ColumnsMap>(`/api/workflows/${workflowId}/columns`).then(setColsMap).catch(() => {})
  }, [workflowId, nodeId])
  const nodeCols = (nodeId && colsMap[nodeId]) || { input: [], output: [] }
  const inputCols = nodeCols.input
  const outputCols = type === 'llm_synth' || type === 'auto_process'
    ? liveOutput(type, config, inputCols) : nodeCols.output
  const canInsert = type === 'llm_synth' || type === 'qc'
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols}
                onInsert={canInsert
                  ? (c) => onChange({ ...config, user_prompt: (config.user_prompt ?? '') + `{{${c}}}` })
                  : undefined} />
  )
  switch (type) {
    case 'input':
      return <InputNodeForm config={config} onChange={onChange} />
    case 'llm_synth':
      return <>{bar}<LlmSynthForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    case 'auto_process':
      return <>{bar}<AutoProcessForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    case 'qc':
      return <>{bar}<QcForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    case 'output':
      return <>{bar}<OutputNodeForm config={config} onChange={onChange} /></>
    default:
      return null
  }
}
```

> 说明：本任务暂不把 `inputCols` 传给 `LlmSynthForm`/`QcForm`（它们的形参与用法在 Task 9 加），故本任务自身可独立通过构建；`inputCols`/`outputCols` 已被顶部 `ColumnsBar` 使用，无未用变量报错。Task 9 再把 `inputCols={inputCols}` 接到这两个 case 上。

- [ ] **Step 4: Build + 单测门禁（本任务应独立绿）**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build 2>&1 | tail -3 && npx vitest run 2>&1 | tail -5`
Expected: `✓ built in …`（无 TS 报错）；vitest 既有 11 单测全绿。顶部出现输入/输出列展示条。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(ui): 节点配置取列 + 输入/输出列展示条与可点插入" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 前端 — llm json 输出列 / agent 产出列 / 质检参数 / 缺列告警

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`

- [ ] **Step 1: `LlmSynthForm` 接 `inputCols` + json 输出列 + 缺列告警**

1. 在 `NodeConfigForm` 的 switch 把 `case 'llm_synth'` 一行改为传 `inputCols`：

```typescript
    case 'llm_synth':
      return <>{bar}<LlmSynthForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
```

2. `LlmSynthForm` 函数签名改为：

```typescript
function LlmSynthForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
```

3. 在「User Prompt」`Field` 的 `</Field>` 之前、`Input.TextArea` 之后插入缺列告警：

```typescript
        <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
```

4. 在「输出方式」`Radio.Group` 的 `Field` 之后，新增 json 模式输出列字段（仅 json 模式显示）：

```typescript
      {(config.output_mode ?? 'column') === 'json' && (
        <Field label="JSON 输出列（解析后拆出的列名，供下游识别）">
          <Select mode="tags" style={{ width: '100%' }} value={config.output_columns ?? []}
                  onChange={(v) => patch({ output_columns: v })} placeholder="如 q_en、category_en" />
        </Field>
      )}
```

- [ ] **Step 2: `QcForm` 接 `inputCols` + 参数块 + 缺列告警**

1. 在 `NodeConfigForm` 的 switch 把 `case 'qc'` 一行改为传 `inputCols`：

```typescript
    case 'qc':
      return <>{bar}<QcForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
```

2. `QcForm` 函数签名改为：

```typescript
function QcForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
```

3. 函数体内 `patch` 之后加 `patchParams`：

```typescript
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
```

4. 在「User Prompt」`Field` 内 `Input.TextArea` 之后插入缺列告警：

```typescript
        <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
```

5. 在「最多回扫轮数」`Field` 之后、说明 `div` 之前，加参数块（不含 json_mode 开关）：

```typescript
      <Space wrap>
        <Field label="temperature"><InputNumber min={0} max={2} step={0.1} value={params.temperature}
          onChange={(v) => patchParams({ temperature: v })} /></Field>
        <Field label="top_p"><InputNumber min={0} max={1} step={0.05} value={params.top_p}
          onChange={(v) => patchParams({ top_p: v })} /></Field>
        <Field label="max_tokens"><InputNumber min={1} value={params.max_tokens}
          onChange={(v) => patchParams({ max_tokens: v })} /></Field>
        <Field label="超时(秒)"><InputNumber min={1} value={params.timeout ?? 120}
          onChange={(v) => patchParams({ timeout: v ?? 120 })} /></Field>
      </Space>
      <div style={{ color: '#999', fontSize: 12 }}>判定默认 temperature 0（确定性）；留空即用 0。</div>
```

- [ ] **Step 3: `AgentOpFields` 存 `output_columns` + 可编辑产出列 tags**

1. 删除 `const [cols, setCols] = useState<string[]>([])` 这一行。
2. `generate()` 里把 `update({ code: r.code }); setCols(r.columns)` 改为：

```typescript
      update({ code: r.code, output_columns: r.output_columns })
```

3. 删除底部 `{cols.length > 0 && (... 检测到的上游列 ...)}` 整块（上游列已由顶部 ColumnsBar 展示）。
4. 在 `{op.code && (...)}` 代码框之后，新增产出列编辑：

```typescript
      {op.code && (
        <div style={{ marginTop: 8 }}>
          <div style={{ color: '#666', fontSize: 12, marginBottom: 4 }}>产出列（本操作新增的列，AI 已填，可改）</div>
          <Select mode="tags" style={{ width: '100%' }} value={op.output_columns ?? []}
                  onChange={(v) => update({ output_columns: v })} placeholder="如 q_english" />
        </div>
      )}
```

- [ ] **Step 4: `OP_DEFAULTS.agent` 加默认 output_columns**

把：

```typescript
  agent: { op: 'agent', instruction: '', code: '' },
```

改为：

```typescript
  agent: { op: 'agent', instruction: '', code: '', output_columns: [] },
```

- [ ] **Step 5: Build + 单测门禁**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build 2>&1 | tail -5 && npx vitest run 2>&1 | tail -8`
Expected: `✓ built in …`（无 TS 报错）；vitest 既有 11 单测全绿。

- [ ] **Step 6: Manual smoke（实现者自检，描述即可）**

打开 workflow 2：
- `llm_synth_1`（column 模式 output_column=a）顶部应显示「输出列：…、a」，其 User/System 不引用缺列；
- 若把它改 json 模式并在「JSON 输出列」填 `q_en、category_en`，下游 `qc_1` 顶部「输入列」应随保存刷新出现 `q_en/category_en`，`qc_1` 的 `{{q_en}}`/`{{category_en}}` 告警消失；
- `auto_process_1` 点「生成代码」后出现可编辑「产出列」；
- `qc_1` 出现 temperature/top_p 等参数。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(ui): llm json 输出列/agent 产出列/质检参数/缺列告警" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 最终回归

- [ ] **后端全量**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q 2>&1 | tail -15`
Expected: 全绿（既有 272 + 本批新增）。

- [ ] **前端全量**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build 2>&1 | tail -3 && npx vitest run 2>&1 | tail -5`
Expected: build ✓、vitest 全绿。

---

## 自检对照（spec 覆盖）

- ①质检可配参数 → Task 7（后端默认可覆盖）+ Task 9 Step 2（QcForm 参数块）。
- ②静态血缘 → Task 1（传播）+ Task 3（codegen 复用）+ Task 6（端点）+ Task 8（展示）。
- ②自声明（json 输出列 / agent 产出列）→ Task 4（codegen 返回 output_columns）+ Task 5（node-assist）+ Task 9 Step 1/3（前端字段）。
- ②缺列软提示 → Task 8（helper）+ Task 9 Step 1/2（llm/qc 告警）。
- 租户隔离 → Task 2（resolve 跳外来）+ Task 3（gather wf.user_id 校验）+ Task 6（端点 404 测试）。
- 不加表不加列 → 全程仅改 JSON config 可选键 + 新增只读端点，无 migration。
