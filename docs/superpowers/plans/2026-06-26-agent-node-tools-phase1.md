# Phase 1：节点操作结构化工具 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 GraphFlow 的 workflow/node/edge 操作封装成主 RedLotus Agent 可直接调用的细粒度 pydantic-ai 工具，并把散在 gf CLI 的图变更逻辑单点化到共享 `graph_ops`。

**Architecture:** 三层——(1) 纯函数 `app/services/graph_ops.py`（对 graph dict 做变更，不碰 DB，失败抛 `GraphOpError`）；(2) `app/services/workflow_store.py` 的 `update_workflow_graph`（落库 + 发 `workflow` SSE 事件）与 `resolve_ref`（名/ID→id）；(3) `app/agent/graph_tools.py` 的 `GraphToolkit`（直连 DB + 归属校验的工具，复用前两层）。gf CLI 改为调 `graph_ops`（去重）；workflows 路由 PUT 改为 delegate `update_workflow_graph`；主 Agent 在 `system.py` 追加工具。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / pydantic-ai / pytest（async）。

## Global Constraints

- KISS、复用优先、单点化、无防御性代码、**禁止任何 dry_run/假运行**。
- 工具直连 DB 用 `(session_factory, user_id)` 范式（对照 `app/agent/catalog.py`）；归属不符返回人话错误串「工作流不存在」，**不抛异常到框架**。
- 工具一律显式收 `workflow_id: int`，不引入隐藏「当前工作流」态。
- `set_node_config` 接受的键与 `gf node set` 完全一致（见 §3.1 of spec）。
- 提交信息**不出现 claude**、不加 Co-Authored-By；提交在分支 `agent-node-tools-phase1` 上。
- 测试：`cd backend && python -m pytest`。每个 Task 末提交一次。
- 设计依据：`docs/superpowers/specs/2026-06-26-agent-node-tools-phase1-design.md`。

---

## 文件结构

- **Create** `backend/app/services/graph_ops.py` — 纯图变更函数 + 常量（结构变更 + 节点配置应用 + op 构建）。
- **Create** `backend/app/services/workflow_store.py` — `update_workflow_graph` + `resolve_ref`。
- **Create** `backend/app/agent/graph_tools.py` — `GraphToolkit`（写 + 读工具）。
- **Modify** `backend/app/routers/workflows.py` — PUT delegate `update_workflow_graph`。
- **Modify** `backend/app/cli/client.py` — 图变更常量/函数迁出后从 `graph_ops` 导入。
- **Modify** `backend/app/cli/commands/node.py` — `node set`/`op` 调 `graph_ops`。
- **Modify** `backend/app/cli/commands/workflow.py` — `node add/rm`/`link`/`unlink` 调 `graph_ops`。
- **Modify** `backend/app/agent/system.py` — `_make_tools` 追加 `GraphToolkit` + catalog 工具。
- **Create** `backend/tests/test_graph_ops.py` — 纯函数单测。
- **Create** `backend/tests/test_workflow_store.py` — service 单测。
- **Create** `backend/tests/test_graph_tools.py` — 工具单测（含跨租户）。
- 回归：`backend/tests/test_cli.py`、`test_cli_node_robustness.py`、`test_workflows.py` 须续绿。

---

## Task 1：`graph_ops` 图结构变更（纯函数）

**Files:**
- Create: `backend/app/services/graph_ops.py`
- Test: `backend/tests/test_graph_ops.py`

**Interfaces:**
- Produces:
  - `class GraphOpError(ValueError)`
  - `NODE_TYPES: dict[str,str]`（`input/llm/auto/output/qc/http` 等 → 规范类型）
  - `find_node(graph: dict, node_id: str) -> dict`（找不到抛 `GraphOpError`）
  - `add_node(graph: dict, node_type: str, node_id: str | None = None) -> str`（返回最终 id）
  - `remove_node(graph: dict, node_id: str) -> None`
  - `connect(graph: dict, source: str, target: str, kind: str) -> None`
  - `disconnect(graph: dict, source: str, target: str) -> None`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_graph_ops.py
import pytest
from app.services import graph_ops as go


def _g():
    return {"nodes": [], "edges": []}


def test_add_node_autoid_and_default_shape():
    g = _g()
    nid = go.add_node(g, "llm")
    assert nid == "llm_synth_1"
    n = g["nodes"][0]
    assert n["id"] == "llm_synth_1" and n["type"] == "llm_synth" and n["config"] == {}
    assert set(n["position"]) == {"x", "y"}


def test_add_node_explicit_id_dup_raises():
    g = _g()
    go.add_node(g, "input", "in")
    with pytest.raises(go.GraphOpError, match="已存在"):
        go.add_node(g, "input", "in")


def test_add_node_unknown_type_raises():
    with pytest.raises(go.GraphOpError, match="未知节点类型"):
        go.add_node(_g(), "banana")


def test_remove_node_drops_incident_edges():
    g = {"nodes": [{"id": "a", "type": "input", "config": {}},
                   {"id": "b", "type": "output", "config": {}}],
         "edges": [{"source": "a", "target": "b", "kind": "normal"}]}
    go.remove_node(g, "a")
    assert [n["id"] for n in g["nodes"]] == ["b"]
    assert g["edges"] == []


def test_remove_node_missing_raises():
    with pytest.raises(go.GraphOpError):
        go.remove_node(_g(), "x")


def test_connect_normal_and_dup_raises():
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "output", "config": {}}], "edges": []}
    go.connect(g, "a", "b", "normal")
    assert g["edges"] == [{"source": "a", "target": "b", "kind": "normal"}]
    with pytest.raises(go.GraphOpError, match="已存在"):
        go.connect(g, "a", "b", "normal")


def test_connect_rescan_must_start_from_qc():
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "llm_synth", "config": {}}], "edges": []}
    with pytest.raises(go.GraphOpError, match="qc"):
        go.connect(g, "a", "b", "rescan")


def test_disconnect_removes_and_missing_raises():
    g = {"nodes": [], "edges": [{"source": "a", "target": "b", "kind": "normal"}]}
    go.disconnect(g, "a", "b")
    assert g["edges"] == []
    with pytest.raises(go.GraphOpError):
        go.disconnect(g, "a", "b")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_ops.py -q`
Expected: FAIL（`No module named 'app.services.graph_ops'`）

- [ ] **Step 3: 写实现**

```python
# backend/app/services/graph_ops.py
"""GraphFlow 图变更纯函数：对 graph dict（{"nodes":[...], "edges":[...]}）做结构/配置变更，
不碰 DB。失败抛 GraphOpError。gf CLI 与 Agent GraphToolkit 共用此单点（去重）。"""


class GraphOpError(ValueError):
    """图变更非法（节点不存在、类型未知、边重复、未知配置键等）。调用方 catch 后 die/返回错误串。"""


NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "qc": "qc", "llm_synth": "llm_synth", "auto_process": "auto_process",
              "http": "http_fetch", "http_fetch": "http_fetch"}


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    raise GraphOpError(f"节点 {node_id} 不存在")


def add_node(graph: dict, node_type: str, node_id: str | None = None) -> str:
    ntype = NODE_TYPES.get(node_type)
    if ntype is None:
        raise GraphOpError(f"未知节点类型 {node_type}（可选: input/llm/auto/output/qc/http）")
    nodes = graph["nodes"]
    if node_id:
        if any(n["id"] == node_id for n in nodes):
            raise GraphOpError(f"节点 {node_id} 已存在")
    else:
        i = 1
        while any(n["id"] == f"{ntype}_{i}" for n in nodes):
            i += 1
        node_id = f"{ntype}_{i}"
    nodes.append({"id": node_id, "type": ntype,
                  "position": {"x": 80 + len(nodes) * 50, "y": 80 + len(nodes) * 40},
                  "config": {}})
    return node_id


def remove_node(graph: dict, node_id: str) -> None:
    find_node(graph, node_id)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]
    graph["edges"] = [e for e in graph["edges"] if node_id not in (e["source"], e["target"])]


def connect(graph: dict, source: str, target: str, kind: str) -> None:
    src = find_node(graph, source)
    find_node(graph, target)
    if kind == "rescan" and src["type"] != "qc":
        raise GraphOpError("rescan 回扫边必须从 qc 节点出发")
    if any(e["source"] == source and e["target"] == target for e in graph["edges"]):
        raise GraphOpError("连线已存在")
    graph["edges"].append({"source": source, "target": target, "kind": kind})


def disconnect(graph: dict, source: str, target: str) -> None:
    before = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"]
                      if not (e["source"] == source and e["target"] == target)]
    if len(graph["edges"]) == before:
        raise GraphOpError(f"不存在连线 {source} -> {target}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_ops.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/graph_ops.py backend/tests/test_graph_ops.py
git commit -m "feat(engine): graph_ops 图结构变更纯函数(add/remove/connect/disconnect)"
```

---

## Task 2：`graph_ops` 节点配置应用 + auto_process 操作

**Files:**
- Modify: `backend/app/services/graph_ops.py`
- Test: `backend/tests/test_graph_ops.py`

**Interfaces:**
- Consumes: Task 1 的 `GraphOpError`、`find_node`。
- Produces:
  - `RESOLVE_KEYS: dict[str, tuple[str, bool]]` = `{"dataset": ("datasets", True), "model": ("models", False), "judge_models": ("models", True)}`（键 → (resolve 资源类型, 是否列表)）
  - `apply_node_config(node: dict, key: str, value) -> None`（键→config 字段映射 + 类型转换；resolve 键期望已解析的 id 值；未知键抛 `GraphOpError`）
  - `build_op(op: str, params: list[str]) -> dict`
  - `add_op(node: dict, op: str, params: list[str]) -> dict`（仅 auto_process；返回新增 op）
  - `remove_op(node: dict, index: int) -> dict`（1-based；越界抛 `GraphOpError`；返回删除的 op）
  - `OP_LABELS: dict[str,str]`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_graph_ops.py
def _node(t="llm_synth"):
    return {"id": "n", "type": t, "config": {}}


def test_apply_llm_config_and_params():
    n = _node()
    go.apply_node_config(n, "prompt", "你好 {{q}}")
    go.apply_node_config(n, "out", "ans")
    go.apply_node_config(n, "fanout", "2")
    go.apply_node_config(n, "temp", "0.7")
    c = n["config"]
    assert c["user_prompt"] == "你好 {{q}}" and c["output_column"] == "ans"
    assert c["fanout_n"] == 2 and c["params"]["temperature"] == 0.7


def test_apply_resolve_keys_expect_ids():
    n = _node()
    go.apply_node_config(n, "model", 7)            # 已解析 id
    go.apply_node_config(n, "dataset", [3, 4])     # 已解析 id 列表
    assert n["config"]["model_config_id"] == 7 and n["config"]["dataset_ids"] == [3, 4]


def test_apply_extract_dict_or_string():
    n = _node("http_fetch")
    go.apply_node_config(n, "extract", "who:name,yr:age")
    assert n["config"]["extract"] == {"who": "name", "yr": "age"}
    go.apply_node_config(n, "extract", {"x": "y"})  # dict 直接用
    assert n["config"]["extract"] == {"x": "y"}


def test_apply_count_empty_means_none():
    n = _node("output")
    go.apply_node_config(n, "count", "5")
    assert n["config"]["count"] == 5
    go.apply_node_config(n, "count", "")
    assert n["config"]["count"] is None


def test_apply_think_and_unknown_key():
    n = _node()
    go.apply_node_config(n, "think", "on")
    assert n["config"]["params"]["thinking_enabled"] is True
    with pytest.raises(go.GraphOpError, match="未知配置键"):
        go.apply_node_config(n, "nope", "x")


def test_add_and_remove_op():
    n = _node("auto_process")
    op = go.add_op(n, "dedup", ["q,a"])
    assert op == {"op": "dedup", "columns": ["q", "a"]}
    assert n["config"]["operations"] == [op]
    removed = go.remove_op(n, 1)
    assert removed["op"] == "dedup" and n["config"]["operations"] == []


def test_add_op_non_auto_raises():
    with pytest.raises(go.GraphOpError, match="auto"):
        go.add_op(_node("llm_synth"), "shuffle", [])


def test_remove_op_index_out_of_range():
    n = _node("auto_process")
    go.add_op(n, "shuffle", [])
    with pytest.raises(go.GraphOpError, match="序号"):
        go.remove_op(n, 5)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_ops.py -q`
Expected: FAIL（`apply_node_config` 等未定义）

- [ ] **Step 3: 写实现（追加到 graph_ops.py）**

```python
# 追加到 backend/app/services/graph_ops.py

LLM_CONFIG_KEYS = {"system": "system_prompt", "prompt": "user_prompt", "out": "output_column",
                   "mode": "output_mode", "fanout": "fanout_n", "conc": "concurrency",
                   "retries": "retries"}
LLM_PARAM_KEYS = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
                  "timeout": "timeout", "json_mode": "json_mode"}
INT_KEYS = {"fanout_n", "concurrency", "retries", "max_tokens", "timeout"}
FLOAT_KEYS = {"temperature", "top_p"}
HTTP_STR_KEYS = {"url", "endpoint", "method", "body"}
RESOLVE_KEYS = {"dataset": ("datasets", True), "model": ("models", False),
                "judge_models": ("models", True)}
OP_LABELS = {"dedup": "去重", "filter": "过滤", "rename": "重命名", "drop": "删除列",
             "concat": "拼接列", "cast": "类型转换", "sample": "随机采样", "shuffle": "打乱"}


def _convert(field: str, v):
    if field in INT_KEYS:
        return int(v)
    if field in FLOAT_KEYS:
        return float(v)
    if field == "json_mode":
        return str(v).lower() in ("true", "1", "yes")
    return v


def _parse_colon_map(v, key: str, fmt: str) -> dict:
    if isinstance(v, dict):
        return v
    out = {}
    for seg in str(v).split(","):
        if not seg.strip():
            continue
        if ":" not in seg:
            raise GraphOpError(f"{key} 格式应为 {fmt}[,{fmt}]，缺少冒号: {seg!r}")
        k, val = seg.split(":", 1)
        out[k] = val
    return out


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x)]
    return [c for c in str(v).split(",") if c]


def apply_node_config(node: dict, key: str, value) -> None:
    """把一对 key/value 落到 node["config"]。resolve 键（dataset/model/judge_models）期望
    value 已是解析好的 id / id 列表（解析在调用方做，本函数不碰 DB）。未知键抛 GraphOpError。"""
    cfg = node["config"]
    if key == "dataset":
        cfg["dataset_ids"] = value if isinstance(value, list) else [value]
    elif key == "model":
        cfg["model_config_id"] = value
    elif key == "judge_models":
        cfg["judge_model_ids"] = value if isinstance(value, list) else [value]
    elif key == "save_as":
        cfg["save_as_dataset"] = bool(value)
        cfg["dataset_name"] = value
    elif key == "pass_k":
        cfg["pass_k"] = int(value)
    elif key == "max_rounds":
        cfg["max_rounds"] = int(value)
    elif key == "count":
        cfg["count"] = int(value) if value not in ("", None) else None
    elif key in HTTP_STR_KEYS:
        cfg[key] = value
    elif key == "extract":
        cfg["extract"] = _parse_colon_map(value, "extract", "列:JSON路径")
    elif key == "headers":
        cfg["headers"] = _parse_colon_map(value, "headers", "名:值")
    elif key in LLM_CONFIG_KEYS:
        cfg[LLM_CONFIG_KEYS[key]] = _convert(LLM_CONFIG_KEYS[key], value)
    elif key in LLM_PARAM_KEYS:
        cfg.setdefault("params", {})[LLM_PARAM_KEYS[key]] = _convert(LLM_PARAM_KEYS[key], value)
    elif key == "drop":
        cfg["drop_columns"] = _as_list(value)
    elif key == "outs":
        cfg["output_columns"] = _as_list(value)
    elif key == "status_col":
        cfg["status_column"] = value
    elif key == "feedback_col":
        cfg["feedback_column"] = value
    elif key == "think":
        cfg.setdefault("params", {})["thinking_enabled"] = str(value).lower() in ("on", "true", "1", "yes")
    elif key == "effort":
        cfg.setdefault("params", {})["reasoning_effort"] = value
    else:
        raise GraphOpError(f"未知配置键 {key}")


def build_op(op: str, params: list[str]) -> dict:
    if op == "dedup":
        return {"op": "dedup", "columns": params[0].split(",") if params else []}
    if op == "filter":
        if len(params) != 3:
            raise GraphOpError("filter 用法: <列> <min_len|max_len|contains|not_contains|regex> <值>")
        col, mode, value = params
        return {"op": "filter", "column": col, "mode": mode,
                "value": int(value) if mode in ("min_len", "max_len") else value}
    if op == "rename":
        if len(params) != 2:
            raise GraphOpError("rename 用法: <原列> <新列>")
        return {"op": "rename", "mapping": {params[0]: params[1]}}
    if op == "drop":
        if len(params) != 1:
            raise GraphOpError("drop 用法: <列1,列2>")
        return {"op": "drop", "columns": params[0].split(",")}
    if op == "concat":
        if len(params) < 2:
            raise GraphOpError("concat 用法: <列1,列2> <目标列> [分隔符]")
        return {"op": "concat", "columns": params[0].split(","), "target": params[1],
                "sep": params[2] if len(params) > 2 else ""}
    if op == "cast":
        if len(params) != 2 or params[1] not in ("str", "int", "float"):
            raise GraphOpError("cast 用法: <列> <str|int|float>")
        return {"op": "cast", "column": params[0], "to": params[1]}
    if op == "sample":
        if len(params) != 1:
            raise GraphOpError("sample 用法: <n>")
        return {"op": "sample", "n": int(params[0])}
    if op == "shuffle":
        return {"op": "shuffle"}
    raise GraphOpError(f"未知操作 {op}（可选: dedup/filter/rename/drop/concat/cast/sample/shuffle）")


def add_op(node: dict, op: str, params: list[str]) -> dict:
    if node["type"] != "auto_process":
        raise GraphOpError(f"{node['id']} 不是自动处理(auto_process)节点")
    built = build_op(op, params)
    node["config"].setdefault("operations", []).append(built)
    return built


def remove_op(node: dict, index: int) -> dict:
    if node["type"] != "auto_process":
        raise GraphOpError(f"{node['id']} 不是自动处理(auto_process)节点")
    ops = node["config"].setdefault("operations", [])
    if not 1 <= index <= len(ops):
        raise GraphOpError(f"序号超出范围（1-{len(ops)}）")
    return ops.pop(index - 1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_ops.py -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/graph_ops.py backend/tests/test_graph_ops.py
git commit -m "feat(engine): graph_ops 节点配置应用 + auto_process op 构建"
```

---

## Task 3：`workflow_store` 服务（落库 + SSE + 名解析）

**Files:**
- Create: `backend/app/services/workflow_store.py`
- Test: `backend/tests/test_workflow_store.py`

**Interfaces:**
- Consumes: `app.models.Workflow/Dataset/ModelConfig/Prompt`、`app.events.publish`、`graph_ops.GraphOpError`。
- Produces:
  - `async def update_workflow_graph(session, wf, graph: dict) -> None`（写 `graph_json` + commit + `publish(wf.user_id, "workflow", wf.id)`）
  - `async def resolve_ref(session, user_id: int, kind: str, ref) -> int`（kind ∈ workflows/datasets/models/prompts；纯数字按 id，否则按 `name` 精确匹配；0 个或多个匹配抛 `GraphOpError`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_workflow_store.py
import json
import pytest
from app.models import ModelConfig, User, Workflow
from app.services import graph_ops as go
from app.services.workflow_store import resolve_ref, update_workflow_graph


async def _seed(sf):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="链路A", graph_json=json.dumps({"nodes": [], "edges": []}))
        s.add(wf)
        s.add(ModelConfig(user_id=u.id, name="通义", model_name="qwen", base_url="http://x/v1",
                          provider="openai", api_key_enc="", default_params_json="{}"))
        await s.flush()
        ids = (u.id, wf.id)
        await s.commit()
    return ids


async def test_update_workflow_graph_persists(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        wf = await s.get(Workflow, wf_id)
        graph = json.loads(wf.graph_json)
        go.add_node(graph, "input", "in")
        await update_workflow_graph(s, wf, graph)
    async with sf() as s:
        wf = await s.get(Workflow, wf_id)
        assert [n["id"] for n in json.loads(wf.graph_json)["nodes"]] == ["in"]


async def test_resolve_ref_by_id_and_name(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    async with sf() as s:
        assert await resolve_ref(s, uid, "workflows", str(wf_id)) == wf_id
        mid = await resolve_ref(s, uid, "models", "通义")
        assert isinstance(mid, int)


async def test_resolve_ref_missing_raises(session_factory):
    sf = session_factory
    uid, _ = await _seed(sf)
    async with sf() as s:
        with pytest.raises(go.GraphOpError, match="找不到"):
            await resolve_ref(s, uid, "models", "不存在的模型")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_workflow_store.py -q`
Expected: FAIL（`No module named 'app.services.workflow_store'`）

- [ ] **Step 3: 写实现**

```python
# backend/app/services/workflow_store.py
"""工作流落库 + 名/ID 解析的服务单点。workflows 路由 PUT 与 Agent GraphToolkit 共用，
保证「落库 + 发 workflow SSE 事件」一条路径（画布据事件调和）。"""
import json

from sqlalchemy import select

from app.events import publish
from app.models import Dataset, ModelConfig, Prompt, Workflow
from app.services.graph_ops import GraphOpError

_KIND_MODEL = {"workflows": Workflow, "datasets": Dataset, "models": ModelConfig, "prompts": Prompt}
_KIND_LABEL = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置", "prompts": "提示词"}


async def update_workflow_graph(session, wf: Workflow, graph: dict) -> None:
    wf.graph_json = json.dumps(graph, ensure_ascii=False)
    await session.commit()
    publish(wf.user_id, "workflow", wf.id)


async def resolve_ref(session, user_id: int, kind: str, ref) -> int:
    """纯数字按 id（仍校验归属），否则按 name 精确匹配本租户资源。0/多个匹配抛 GraphOpError。"""
    model = _KIND_MODEL[kind]
    s = str(ref)
    if s.isdigit():
        obj = await session.get(model, int(s))
        if obj is None or obj.user_id != user_id:
            raise GraphOpError(f"找不到 id={s} 的{_KIND_LABEL[kind]}")
        return int(s)
    hits = (await session.execute(
        select(model).where(model.user_id == user_id, model.name == s))).scalars().all()
    if len(hits) == 1:
        return hits[0].id
    if not hits:
        raise GraphOpError(f"找不到名为「{s}」的{_KIND_LABEL[kind]}")
    raise GraphOpError(f"「{s}」有 {len(hits)} 个同名{_KIND_LABEL[kind]}，请改用 id: {[h.id for h in hits]}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_workflow_store.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/workflow_store.py backend/tests/test_workflow_store.py
git commit -m "feat(workflow): workflow_store 落库+SSE 单点 + resolve_ref 名解析"
```

---

## Task 4：workflows 路由 PUT delegate `update_workflow_graph`

**Files:**
- Modify: `backend/app/routers/workflows.py:111-121`
- Test: 回归 `backend/tests/test_workflows.py`

**Interfaces:**
- Consumes: Task 3 的 `update_workflow_graph`。

- [ ] **Step 1: 跑现有回归基线**

Run: `cd backend && python -m pytest tests/test_workflows.py -q`
Expected: PASS（记录当前通过数）

- [ ] **Step 2: 改 PUT 处理器**

在 `workflows.py` 顶部 import 区追加：
```python
from app.services.workflow_store import update_workflow_graph
```

把 `update_workflow`（111-121）的图分支改为 delegate：
```python
@router.put("/{wf_id}")
async def update_workflow(wf_id: int, body: WorkflowUpdate, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    if body.name is not None:
        wf.name = body.name
    if body.graph is not None:
        await update_workflow_graph(session, wf, body.graph)
    else:
        await session.commit()
        publish(user.id, "workflow", wf.id)
    return _out(wf)
```
（仅改名时仍 commit + publish，与原行为一致；改图走单点 service。）

- [ ] **Step 3: 跑回归确认不变**

Run: `cd backend && python -m pytest tests/test_workflows.py tests/test_events.py -q`
Expected: PASS（与 Step 1 同通过数）

- [ ] **Step 4: 提交**

```bash
git add backend/app/routers/workflows.py
git commit -m "refactor(workflow): PUT 图更新 delegate update_workflow_graph 单点"
```

---

## Task 5：gf CLI 改用 `graph_ops`（去重，行为不变）

**Files:**
- Modify: `backend/app/cli/client.py`
- Modify: `backend/app/cli/commands/node.py`
- Modify: `backend/app/cli/commands/workflow.py`
- Test: 回归 `backend/tests/test_cli.py`、`test_cli_node_robustness.py`、`test_prompt_cli.py`

**Interfaces:**
- Consumes: `graph_ops` 全部（Task 1/2）。

- [ ] **Step 1: 跑 CLI 回归基线**

Run: `cd backend && python -m pytest tests/test_cli.py tests/test_cli_node_robustness.py tests/test_prompt_cli.py -q`
Expected: PASS（记录通过数）

- [ ] **Step 2: `client.py` —— 删除迁出的重复定义，改为 re-export**

`client.py` 中删除 `NODE_TYPES`、`LLM_CONFIG_KEYS`、`LLM_PARAM_KEYS`、`INT_KEYS`、`FLOAT_KEYS`、`HTTP_STR_KEYS`、`OP_LABELS`、`convert`、`find_node`、`build_op` 的本地定义（已迁入 graph_ops）。在 import 区追加：
```python
from app.services.graph_ops import (  # 单点复用，避免与 graph_ops 重复定义
    NODE_TYPES, LLM_CONFIG_KEYS, LLM_PARAM_KEYS, HTTP_STR_KEYS, OP_LABELS,
    build_op, find_node, GraphOpError)
```
`convert` 的外部引用（`cli/commands/model.py` 等若有）改为：在 client.py 保留薄封装 `def convert(field, v): return graph_ops._convert(field, v)`，或把这些调用点改成不依赖（**先 grep** `from app.cli.client import` 找出 `convert` 的导入方，逐一改导入源）。
`parse_kv`、`resolve`、`_auto_node`、`_parse_colon_map` 等 CLI 专有的保留在 client.py（CLI 仍需）。

> 注意 `_parse_colon_map` 现同时存在于 `cli/commands/node.py`，统一改为从 graph_ops 导入（graph_ops 版接受 dict-or-string，对 CLI 的 string 入参行为一致）。

- [ ] **Step 3: `cli/commands/workflow.py` —— `node add/rm`、`link`、`unlink` 改调 graph_ops**

`cmd_node_add`：
```python
def cmd_node_add(args):
    cli = Cli()
    wf = cli.get_wf()
    try:
        node_id = graph_ops.add_node(wf["graph"], args.type, args.id)
    except graph_ops.GraphOpError as e:
        die(str(e))
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加节点 {node_id}")
```
`cmd_node_rm` / `cmd_link` / `cmd_unlink` 同样：取 `wf=cli.get_wf()`，`try: graph_ops.remove_node/connect/disconnect(...) except GraphOpError as e: die(str(e))`，再 `cli.put_graph`，print 不变。文件顶部加 `from app.services import graph_ops`。删除本文件里对 `find_node` 的直接图操作逻辑（改由 graph_ops 内部处理）。

- [ ] **Step 4: `cli/commands/node.py` —— `node set`、`op add/rm` 改调 graph_ops**

`cmd_node_set`：先用 `cli.resolve` 把 `RESOLVE_KEYS` 的键解析成 id，再调 `graph_ops.apply_node_config`：
```python
def cmd_node_set(args):
    cli = Cli()
    wf = cli.get_wf()
    try:
        node = graph_ops.find_node(wf["graph"], args.id)
        for k, v in parse_kv(args.pairs).items():
            if k in graph_ops.RESOLVE_KEYS:
                kind, is_list = graph_ops.RESOLVE_KEYS[k]
                refs = [r for r in v.split(",") if r] if is_list else [v]
                ids = [cli.resolve(kind, r) for r in refs]
                graph_ops.apply_node_config(node, k, ids if is_list else ids[0])
            else:
                graph_ops.apply_node_config(node, k, v)
    except graph_ops.GraphOpError as e:
        die(str(e))
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已更新 {args.id}: {json.dumps(node['config'], ensure_ascii=False)}")
```
`cmd_op_add` / `cmd_op_rm` 改为用 `graph_ops.add_op` / `remove_op`（仍用 `_auto_node` 取 wf+node 或直接 `find_node`；越界/类型错由 graph_ops 抛、catch→die）。`cmd_op_ls` 用 `graph_ops.OP_LABELS`。文件顶部 import 改为从 graph_ops 取 `OP_LABELS/build_op`，并 `from app.services import graph_ops`。

- [ ] **Step 5: 跑 CLI 回归确认行为不变**

Run: `cd backend && python -m pytest tests/test_cli.py tests/test_cli_node_robustness.py tests/test_prompt_cli.py -q`
Expected: PASS（与 Step 1 同通过数；若有差异逐一对照修正，**不得改测试迁就实现**）

- [ ] **Step 6: 提交**

```bash
git add backend/app/cli/
git commit -m "refactor(cli): gf 图变更逻辑改调 graph_ops 单点(去重，行为不变)"
```

---

## Task 6：`GraphToolkit` 写工具

**Files:**
- Create: `backend/app/agent/graph_tools.py`
- Test: `backend/tests/test_graph_tools.py`

**Interfaces:**
- Consumes: `graph_ops`（Task 1/2）、`workflow_store.update_workflow_graph/resolve_ref`（Task 3）、`app.models`、`app.events`。
- Produces: `class GraphToolkit` 含写方法 + `tools` property（Task 7 再加读方法）。本任务先实现写方法与 `tools` 暂列写方法。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_graph_tools.py
import json
import pytest
from app.agent.graph_tools import GraphToolkit
from app.models import Dataset, ModelConfig, User, Workflow


async def _seed(sf, graph=None):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="链路A",
                      graph_json=json.dumps(graph or {"nodes": [], "edges": []}))
        s.add(wf)
        s.add(ModelConfig(user_id=u.id, name="通义", model_name="qwen", base_url="http://x/v1",
                          provider="openai", api_key_enc="", default_params_json="{}"))
        s.add(Dataset(user_id=u.id, name="集A", source="upload", row_count=3,
                      columns_json=json.dumps(["q"])))
        await s.flush()
        ids = (u.id, wf.id)
        await s.commit()
    return ids


async def _graph(sf, wf_id):
    async with sf() as s:
        return json.loads((await s.get(Workflow, wf_id)).graph_json)


async def test_add_node_persists(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    msg = await GraphToolkit(sf, uid).add_node(wf_id, "llm")
    assert "llm_synth_1" in msg
    assert [n["id"] for n in (await _graph(sf, wf_id))["nodes"]] == ["llm_synth_1"]


async def test_connect_and_disconnect(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "output", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    await tk.connect_nodes(wf_id, "a", "b")
    assert (await _graph(sf, wf_id))["edges"] == [{"source": "a", "target": "b", "kind": "normal"}]
    await tk.disconnect_nodes(wf_id, "a", "b")
    assert (await _graph(sf, wf_id))["edges"] == []


async def test_set_node_config_resolves_names(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    await GraphToolkit(sf, uid).set_node_config(wf_id, "g", {"model": "通义", "out": "ans", "prompt": "答 {{q}}"})
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "g")["config"]
    assert isinstance(cfg["model_config_id"], int) and cfg["output_column"] == "ans"


async def test_set_node_config_bad_key_returns_error(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "g", "type": "llm_synth", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    msg = await GraphToolkit(sf, uid).set_node_config(wf_id, "g", {"nope": "x"})
    assert "未知配置键" in msg


async def test_cross_tenant_rejected(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    msg = await GraphToolkit(sf, uid + 999).add_node(wf_id, "input")
    assert msg == "工作流不存在"
    assert (await _graph(sf, wf_id))["nodes"] == []   # 受害数据未被改


async def test_add_op_and_remove(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "p", "type": "auto_process", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    await tk.add_node_op(wf_id, "p", "dedup", ["q"])
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "p")["config"]
    assert cfg["operations"] == [{"op": "dedup", "columns": ["q"]}]
    await tk.remove_node_op(wf_id, "p", 1)
    cfg = next(n for n in (await _graph(sf, wf_id))["nodes"] if n["id"] == "p")["config"]
    assert cfg["operations"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -q`
Expected: FAIL（`No module named 'app.agent.graph_tools'`）

- [ ] **Step 3: 写实现**

```python
# backend/app/agent/graph_tools.py
"""Agent 图操作工具：把 workflow/node/edge 操作做成直连 DB 的 pydantic-ai 工具。
范式同 catalog/node_info（session_factory + user_id + 归属校验）；图变更走 graph_ops 单点，
落库+SSE 走 workflow_store。归属不符返回人话错误串，不抛异常到框架。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Workflow
from app.services import graph_ops as go
from app.services.workflow_store import resolve_ref, update_workflow_graph


class GraphToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int):
        self._sf = session_factory
        self._uid = user_id

    async def _owned(self, session, workflow_id: int):
        wf = await session.get(Workflow, int(workflow_id))
        return wf if wf is not None and wf.user_id == self._uid else None

    async def _mutate(self, workflow_id: int, fn) -> str:
        """取属主工作流→对 graph dict 跑 fn(graph, session)（可 await）→落库+SSE。
        fn 返回成功消息串；GraphOpError→错误串；非属主→「工作流不存在」。"""
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            graph = json.loads(wf.graph_json)
            try:
                msg = await fn(graph, s)
            except go.GraphOpError as e:
                return f"Error: {e}"
            await update_workflow_graph(s, wf, graph)
            return msg

    async def create_workflow(self, name: str) -> str:
        """新建一个空工作流，返回其 id。
        Parameters:
            name: 工作流名称
        """
        async with self._sf() as s:
            wf = Workflow(user_id=self._uid, name=name,
                          graph_json=json.dumps({"nodes": [], "edges": []}, ensure_ascii=False))
            s.add(wf)
            await s.commit()
            from app.events import publish
            publish(self._uid, "workflow", wf.id)
            return f"已创建工作流「{name}」(#{wf.id})"

    async def rename_workflow(self, workflow_id: int, name: str) -> str:
        """重命名工作流。
        Parameters:
            workflow_id: 工作流 id
            name: 新名称
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            wf.name = name
            await s.commit()
            from app.events import publish
            publish(self._uid, "workflow", wf.id)
            return f"已重命名工作流 #{workflow_id} -> {name}"

    async def delete_workflow(self, workflow_id: int) -> str:
        """删除工作流（连带其运行记录由既有级联保证）。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            await s.delete(wf)
            await s.commit()
            from app.events import publish
            publish(self._uid, "workflow", workflow_id)
            return f"已删除工作流 #{workflow_id}"

    async def add_node(self, workflow_id: int, node_type: str, node_id: str | None = None) -> str:
        """给工作流加一个节点，返回其 id。
        Parameters:
            workflow_id: 工作流 id
            node_type: input/llm/auto/output/qc/http 之一
            node_id: 可选指定 id；留空自动生成
        """
        async def fn(graph, _s):
            nid = go.add_node(graph, node_type, node_id)
            return f"已添加节点 {nid}"
        return await self._mutate(workflow_id, fn)

    async def remove_node(self, workflow_id: int, node_id: str) -> str:
        """删除节点及其连线。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
        """
        async def fn(graph, _s):
            go.remove_node(graph, node_id)
            return f"已删除节点 {node_id} 及其连线"
        return await self._mutate(workflow_id, fn)

    async def connect_nodes(self, workflow_id: int, source: str, target: str,
                            kind: str = "normal") -> str:
        """连一条边。kind=normal 普通边；kind=rescan 质检回扫边(必须从 qc 节点出发)。
        Parameters:
            workflow_id: 工作流 id
            source: 源节点 id
            target: 目标节点 id
            kind: normal 或 rescan
        """
        async def fn(graph, _s):
            go.connect(graph, source, target, kind)
            return f"已连线 {source} -> {target}（{kind}）"
        return await self._mutate(workflow_id, fn)

    async def disconnect_nodes(self, workflow_id: int, source: str, target: str) -> str:
        """断开一条边。
        Parameters:
            workflow_id: 工作流 id
            source: 源节点 id
            target: 目标节点 id
        """
        async def fn(graph, _s):
            go.disconnect(graph, source, target)
            return f"已断开 {source} -> {target}"
        return await self._mutate(workflow_id, fn)

    async def set_node_config(self, workflow_id: int, node_id: str, config: dict) -> str:
        """设置节点配置（一次可设多个键）。键同 gf node set：
        model/dataset/judge_models(填名或id，自动解析)、prompt/system(提示词)、out/outs/mode/fanout/
        conc/retries、temp/top_p/max_tokens/timeout/json_mode(采样)、think/effort(思考)、
        url/endpoint/method/body/extract/headers(http)、pass_k/max_rounds/status_col/feedback_col(质检)、
        count(产量上限)、drop(删列)、save_as(存为数据集)。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
            config: {键: 值} 字典
        """
        async def fn(graph, s):
            node = go.find_node(graph, node_id)
            for key, raw in config.items():
                if key in go.RESOLVE_KEYS:
                    kind, is_list = go.RESOLVE_KEYS[key]
                    refs = (raw if isinstance(raw, list) else
                            [r for r in str(raw).split(",") if r]) if is_list else [raw]
                    ids = [await resolve_ref(s, self._uid, kind, r) for r in refs]
                    go.apply_node_config(node, key, ids if is_list else ids[0])
                else:
                    go.apply_node_config(node, key, raw)
            return f"已更新节点 {node_id} 配置"
        return await self._mutate(workflow_id, fn)

    async def set_node_prompt(self, workflow_id: int, node_id: str, slot: str,
                             body: str | None = None, library_ref: int | str | None = None,
                             mode: str = "copy") -> str:
        """设置节点的系统/用户提示词。直接传 body 写内联；或传 library_ref 用库提示词
        (mode=ref 运行时取最新版；mode=copy 复制当前正文进来)。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
            slot: system 或 user
            body: 内联提示词正文（与 library_ref 二选一）
            library_ref: 库提示词 id 或名
            mode: ref(引用) 或 copy(复制，默认)
        """
        if slot not in ("system", "user"):
            return "Error: slot 必须为 system 或 user"
        field = "system_prompt" if slot == "system" else "user_prompt"

        async def fn(graph, s):
            node = go.find_node(graph, node_id)
            cfg = node["config"]
            if library_ref is not None:
                pid = await resolve_ref(s, self._uid, "prompts", library_ref)
                if mode == "ref":
                    cfg[f"{field}_ref"] = pid
                    return f"已将 {node_id} 的 {slot} 提示词设为引用库 #{pid}"
                from app.models import Prompt, PromptVersion
                from sqlalchemy import select as _select
                ver = (await s.execute(_select(PromptVersion)
                       .where(PromptVersion.prompt_id == pid)
                       .order_by(PromptVersion.version.desc()).limit(1))).scalars().first()
                cfg[field] = ver.body if ver else ""
                cfg.pop(f"{field}_ref", None)
                return f"已复制库提示词 #{pid} 到 {node_id} 的 {slot}"
            if body is None:
                raise go.GraphOpError("需提供 body 或 library_ref")
            cfg[field] = body
            cfg.pop(f"{field}_ref", None)
            return f"已写入 {node_id} 的 {slot} 提示词（{len(body)} 字符）"
        return await self._mutate(workflow_id, fn)

    async def add_node_op(self, workflow_id: int, node_id: str, op: str, params: list[str]) -> str:
        """给自动处理节点追加一个操作。op ∈ dedup/filter/rename/drop/concat/cast/sample/shuffle。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
            op: 操作名
            params: 操作参数列表（如 dedup 用 ["列1,列2"]）
        """
        async def fn(graph, _s):
            built = go.add_op(go.find_node(graph, node_id), op, params)
            return f"已添加操作: {json.dumps(built, ensure_ascii=False)}"
        return await self._mutate(workflow_id, fn)

    async def remove_node_op(self, workflow_id: int, node_id: str, index: int) -> str:
        """删除自动处理节点的第 index 个操作（1-based）。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
            index: 操作序号，从 1 开始
        """
        async def fn(graph, _s):
            removed = go.remove_op(go.find_node(graph, node_id), int(index))
            return f"已删除操作: {go.OP_LABELS.get(removed['op'], removed['op'])}"
        return await self._mutate(workflow_id, fn)

    @property
    def tools(self) -> list:
        return [self.create_workflow, self.rename_workflow, self.delete_workflow,
                self.add_node, self.remove_node, self.connect_nodes, self.disconnect_nodes,
                self.set_node_config, self.set_node_prompt, self.add_node_op, self.remove_node_op]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/graph_tools.py backend/tests/test_graph_tools.py
git commit -m "feat(agent): GraphToolkit 写工具(workflow/node/edge/config/prompt/op)"
```

---

## Task 7：`GraphToolkit` 读工具

**Files:**
- Modify: `backend/app/agent/graph_tools.py`
- Test: `backend/tests/test_graph_tools.py`

**Interfaces:**
- Consumes: `app.agent.node_info._summarize_node`、`app.engine.graph.parse_graph`、`app.engine.columns`（列血缘）、`app.agent.data_preview._fit_budget`。
- Produces: `GraphToolkit` 增 `list_workflows()`、`show_workflow_graph(workflow_id)`、`workflow_columns(workflow_id, node_id=None)`、`list_node_ops(workflow_id, node_id)`；并入 `tools`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_graph_tools.py
async def test_list_and_show(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": []}},
                   {"id": "o", "type": "output", "config": {}}],
         "edges": [{"source": "in", "target": "o", "kind": "normal"}]}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid)
    lst = json.loads(await tk.list_workflows())
    assert any(w["id"] == wf_id and w["name"] == "链路A" for w in lst["rows"])
    shown = json.loads(await tk.show_workflow_graph(wf_id))
    assert {n["id"] for n in shown["rows"]} == {"in", "o"}
    assert len(shown["edges"]) == 1


async def test_show_cross_tenant(session_factory):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    out = json.loads(await GraphToolkit(sf, uid + 999).show_workflow_graph(wf_id))
    assert out.get("error") == "workflow_not_found"


async def test_list_node_ops(session_factory):
    sf = session_factory
    g = {"nodes": [{"id": "p", "type": "auto_process",
                    "config": {"operations": [{"op": "shuffle"}]}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    out = json.loads(await GraphToolkit(sf, uid).list_node_ops(wf_id, "p"))
    assert out["rows"][0]["op"] == "shuffle"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -k "list_and_show or cross_tenant or list_node_ops" -q`
Expected: FAIL（方法未定义）

- [ ] **Step 3: 写实现（追加读方法 + 更新 import 与 tools）**

在 `graph_tools.py` 顶部 import 区追加：
```python
from app.agent.data_preview import _fit_budget
from app.agent.node_info import _summarize_node
from app.engine.graph import parse_graph
```

在 `GraphToolkit` 内追加方法：
```python
    async def list_workflows(self) -> str:
        """列本租户全部工作流(id/名)。先用它拿到 workflow_id 再做后续操作。"""
        async with self._sf() as s:
            recs = (await s.execute(select(Workflow).where(Workflow.user_id == self._uid)
                                    .order_by(Workflow.id.desc()))).scalars().all()
            return json.dumps(_fit_budget(
                {"rows": [{"id": w.id, "name": w.name} for w in recs]}), ensure_ascii=False)

    async def show_workflow_graph(self, workflow_id: int) -> str:
        """看某工作流的图：所有节点(id/类型/关键配置摘要)与连线(普通/回扫)。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            try:
                graph = parse_graph(wf.graph_json)
            except Exception:
                return json.dumps({"error": "graph_unparseable"}, ensure_ascii=False)
            nodes = [{"id": n.id, "type": n.type, "config": _summarize_node(n)} for n in graph.nodes]
            edges = [{"source": e["source"], "target": e["target"], "kind": e["kind"]}
                     for e in graph.edges]
            return json.dumps(_fit_budget(
                {"workflow_name": wf.name, "rows": nodes, "edges": edges}, key="rows"),
                ensure_ascii=False)

    async def list_node_ops(self, workflow_id: int, node_id: str) -> str:
        """列自动处理节点的操作序列。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            graph = json.loads(wf.graph_json)
            try:
                node = go.find_node(graph, node_id)
            except go.GraphOpError:
                return json.dumps({"error": "node_not_found"}, ensure_ascii=False)
            ops = node.get("config", {}).get("operations", [])
            return json.dumps({"rows": ops}, ensure_ascii=False)
```

> `workflow_columns` 列血缘：调 `GET /api/workflows/{id}/columns` 等价的内部计算。**先读** `backend/app/routers/workflows.py` 的 columns 端点（约 95-110 行）确认其用 `propagate_columns`/`resolve_dataset_cols`，在 `list_node_ops` 后追加一个 `workflow_columns(workflow_id, node_id=None)` 方法复用同一计算，输出 `{node_id: {"input": [...], "output": [...]}}`（含 `_fit_budget`）。归属不符返回 `{"error": "workflow_not_found"}`。补一条对应单测（`test_workflow_columns_tool`）。

更新 `tools` property，把读方法并入：
```python
    @property
    def tools(self) -> list:
        return [self.list_workflows, self.show_workflow_graph, self.list_node_ops,
                self.workflow_columns,
                self.create_workflow, self.rename_workflow, self.delete_workflow,
                self.add_node, self.remove_node, self.connect_nodes, self.disconnect_nodes,
                self.set_node_config, self.set_node_prompt, self.add_node_op, self.remove_node_op]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/graph_tools.py backend/tests/test_graph_tools.py
git commit -m "feat(agent): GraphToolkit 读工具(list/show/columns/ops)"
```

---

## Task 8：把 GraphToolkit + catalog 工具接进主 Agent

**Files:**
- Modify: `backend/app/agent/system.py:44-48`
- Test: `backend/tests/test_agent_system.py`（或新增 wiring 断言）

**Interfaces:**
- Consumes: `GraphToolkit`（Task 6/7）、`make_catalog_tools`（既有）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_agent_system.py（若无则新建，参照该文件现有夹具构造 AgentSystem）
def test_make_tools_includes_graph_and_catalog(tmp_path):
    from app.agent.system import AgentSystem
    sysm = AgentSystem(models={"coordinator": None, "manager": None, "worker": None},
                       workdir=tmp_path, confirm_delete=False, emit=lambda *a, **k: None,
                       user_id=1, session_factory=lambda: None)
    names = {getattr(t, "__name__", "") for t in sysm._make_tools(tmp_path / "s.json")}
    assert {"add_node", "set_node_config", "connect_nodes", "show_workflow_graph",
            "list_workflows", "list_user_models"} <= names
```
> 若 `AgentSystem.__init__` 因 `models` 含 None 在构造期即访问模型而报错，则改为只测 `_make_tools` 所依赖的装配函数：直接断言 `GraphToolkit(sf, 1).tools` 与 `make_catalog_tools(sf, 1)` 的方法名集合（更稳，避免耦合 AgentSystem 构造）。实现者按实际选其一，保证断言「主 Agent 工具集含上述名字」。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_system.py -k make_tools -q`
Expected: FAIL（工具名不在集合中）

- [ ] **Step 3: 改 `system.py:_make_tools`**

```python
    def _make_tools(self, state_file: Path) -> list:
        tk = AgentToolkit(self.workdir, state_file, self._confirm_delete,
                          session_factory=self._session_factory, user_id=self._user_id)
        sk = SkillsToolkit(self.skills_manager, state_file)
        tools = tk.tools + sk.tools
        if self._session_factory is not None and self._user_id is not None:
            from app.agent.graph_tools import GraphToolkit
            from app.agent.catalog import make_catalog_tools
            tools += GraphToolkit(self._session_factory, self._user_id).tools
            tools += make_catalog_tools(self._session_factory, self._user_id)
        return tools
```
（无 session/user（纯测试构造）时不挂图工具，保持现有 e2e 测试不破。）

- [ ] **Step 4: 跑测试确认通过 + 不破坏 agent 套件**

Run: `cd backend && python -m pytest tests/test_agent_system.py tests/test_agent_turns.py tests/test_agent_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/system.py backend/tests/test_agent_system.py
git commit -m "feat(agent): 主 Agent 装配 GraphToolkit + catalog 工具"
```

---

## Task 9：全量回归 + 收尾

**Files:** 无（验证 + 提交）

- [ ] **Step 1: 跑后端全量**

Run: `cd backend && python -m pytest -q`
Expected: PASS（全绿；新增约 20 个测试）。任何失败逐一定位修复（**不得删/改测试迁就**），尤其确认 `test_cli*`、`test_workflows`、`test_events`、`test_agent_*` 续绿。

- [ ] **Step 2: 死代码/重复核查**

确认 `cli/client.py` 不再有与 `graph_ops` 重复的 `build_op/find_node/NODE_TYPES/key 表/convert` 定义（grep 校验）；`_parse_colon_map` 仅在 graph_ops 一处。

Run: `cd backend && git grep -n "def build_op\|def find_node\|^NODE_TYPES" app/cli`
Expected: 无输出（均已迁出）

- [ ] **Step 3: 提交收尾（若 Step 1/2 有改动）**

```bash
git add -A
git commit -m "test(agent): Phase1 全量回归通过 + 去重核查"
```

---

## 实现后：对抗式复审 + 活体

- 实现完成后用 `superpowers:requesting-code-review`（或多 agent 对抗复审）审 graph_ops 边界/工具归属/CLI 行为漂移。
- 重启后端后人工活体：主 Agent 用工具从零搭一条 input→llm→output 链路并连线/设模型与提示词，跑通；建即删回基线（活体脚本可仿 `backend/tools/http_fetch_polling_live.py`）。
- 合并回 master 前确保全绿；合并后删分支 `agent-node-tools-phase1`。

## Self-Review 记录（写计划时自查）

- **Spec 覆盖**：§3 工具清单 → Task 6/7；§4.1 graph_ops → Task 1/2；§4.2 CLI 去重 → Task 5；§4.3 update_workflow_graph → Task 3/4；§4.4 GraphToolkit + 接入 → Task 6/7/8；§6 测试 → 各 Task TDD + Task 9。`restore/export/import`、节点助手按 spec §7 不在本期。
- **类型一致**：`GraphOpError`/`find_node`/`apply_node_config`/`RESOLVE_KEYS`(键→(kind,is_list))/`update_workflow_graph(session,wf,graph)`/`resolve_ref(session,user_id,kind,ref)`/`GraphToolkit(sf,uid)` 跨 Task 一致。
- **占位符**：无 TODO/TBD；每代码步给完整代码。`workflow_columns` 一处要求实现者先读 columns 端点再照搬计算（已给明确出处与输出契约 + 要求补测），非占位。
