# http_fetch 轮询 + 结果展开 + 起始数据源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `http_fetch` 节点能轮询异步接口直到状态就绪、把返回的记录数组展开成多行、并能作为无 input 节点的工作流起始数据源。

**Architecture:** 轮询循环在 `run_http_fetch_row` 内编排（复用 `http.fetch` 单次原语），展开复用 fanout 的「一 row_idx 挂多行」机制；无上游的 http_fetch 节点从「生成循环」改走普通 topo + 单空种子触发一次取数。三个能力彼此独立、可组合。

**Tech Stack:** Python 3.12 / asyncio / SQLAlchemy async / pytest（asyncio_mode=auto，monkeypatch `app.services.http.fetch`）；前端 React + TypeScript + Ant Design + vitest。

## Global Constraints

- KISS：最简实现，无投机抽象、无防御性代码（不预防未发生的 bug）。
- 复用优先、单点化，不堆补丁式新代码；死代码即清。
- 错误文案只含 method/endpoint/status，**绝不含 headers/body**（防 token 外泄）。
- 列血缘**不改**：http_fetch 输出列恒为 `输入列 ∪ extract.keys`。
- `retries`（单次请求传输层重试）与 `poll_max_attempts`（整体轮询轮次）语义分开，不混用。
- 默认值：`poll_interval=2`、`poll_max_attempts=30`。
- presence-based 开启：`poll_status_path` 非空即开轮询；`records_path` 非空即展开。
- 提交记录不出现 claude，不加 Co-Authored-By 尾注。提交信息用中文。
- 全程在分支 `http-fetch-polling`（已创建）。测试只本地不推 origin。

---

### Task 1: `run_http_fetch_row` 轮询循环

**Files:**
- Modify: `backend/app/engine/nodes.py:226-253`（在 `_CONTENT_TYPES` 后加 `_http_poll`；改 `run_http_fetch_row` 的取数段）
- Test: `backend/tests/test_http_node.py`（追加 3 个测试）

**Interfaces:**
- Consumes: 现有 `json_path_get(obj, path)`、`render_template`、`strip_internal`、`http.fetch(method,url,headers,body,timeout,retries)->(int,str)`、模块级 `_json`/`asyncio`/`httpx`（均已 import）。
- Produces: 新增模块级 `async def _http_poll(config: dict, method: str, url: str, headers: dict, body: str|None) -> dict|list`——未配 `poll_status_path` 时发一次（非 JSON 抛 ValueError）；配了则反复发同一请求直到 `json_path_get(data, poll_status_path)` 的 `str()` 等于 `str(poll_until)`，最多 `poll_max_attempts` 次，耗尽抛 ValueError。`run_http_fetch_row(config, row) -> (list[dict], dict)` 签名不变。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_http_node.py` 末尾追加：

```python
async def test_http_poll_until_status_done(monkeypatch):
    """配了 poll_status_path：反复发同一请求，直到状态字段达 poll_until 才提取。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        if calls["n"] < 3:
            return 200, json.dumps({"status": "pending"})
        return 200, json.dumps({"status": "completed", "result": 42})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 10, "extract": {"r": "result"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 3
    assert out == [{"id": "1", "r": 42}]


async def test_http_poll_exhausts_attempts_raises(monkeypatch):
    """状态恒不就绪：发满 poll_max_attempts 次后抛 ValueError（含「轮询」），由 runner 记为行/run 失败。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        return 200, json.dumps({"status": "pending"})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 3, "extract": {"r": "result"}}
    with pytest.raises(ValueError, match="轮询"):
        await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 3


async def test_http_poll_non_json_treated_as_not_ready(monkeypatch):
    """轮询期间非 JSON 响应（如 202 空体）视为「未就绪」继续轮询，而非立刻失败。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        if calls["n"] < 2:
            return 202, "Accepted"
        return 200, json.dumps({"status": "completed", "v": 1})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 5, "extract": {"v": "v"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 2
    assert out == [{"id": "1", "v": 1}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py::test_http_poll_until_status_done tests/test_http_node.py::test_http_poll_exhausts_attempts_raises tests/test_http_node.py::test_http_poll_non_json_treated_as_not_ready -v`
Expected: FAIL（`run_http_fetch_row` 未识别 poll 参数，第 1 个测试 `calls["n"]==1`；超时测试不抛错）

- [ ] **Step 3: 实现 `_http_poll` 并改 `run_http_fetch_row`**

在 `backend/app/engine/nodes.py` 的 `_CONTENT_TYPES = {...}` 行（第 226 行）之后、`run_http_fetch_row` 之前，插入：

```python
async def _http_poll(config: dict, method: str, url: str, headers: dict, body):
    """取一次 JSON（未配 poll_status_path）或轮询同一请求直到状态就绪（配了）。
    轮询：第 1 次立即发，未就绪则 sleep(poll_interval) 再发，最多 poll_max_attempts 次。
    非 JSON / 状态字段缺失在轮询时视为「未就绪」；耗尽次数抛 ValueError（取数失败、点名）。
    错误文案只含 method/url/status——不含 headers/body（防 token 外泄）。"""
    timeout = config.get("timeout", 30)
    retries = config.get("retries", 2)
    status_path = config.get("poll_status_path")
    if not status_path:                                   # 同步接口：发一次，非 JSON 即抛（现行行为）
        status, text = await http.fetch(method, url, headers=headers, body=body,
                                        timeout=timeout, retries=retries)
        try:
            return _json.loads(text, parse_constant=lambda _v: None)
        except (ValueError, TypeError):
            raise ValueError(f"接口响应非 JSON，无法提取（HTTP {status} {url}）")
    until = str(config.get("poll_until"))
    interval = config.get("poll_interval", 2)
    attempts = config.get("poll_max_attempts", 30)
    for attempt in range(attempts):
        status, text = await http.fetch(method, url, headers=headers, body=body,
                                        timeout=timeout, retries=retries)
        try:
            data = _json.loads(text, parse_constant=lambda _v: None)
        except (ValueError, TypeError):
            data = None                                   # 轮询中非 JSON = 未就绪
        if data is not None and str(json_path_get(data, status_path)) == until:
            return data
        if attempt < attempts - 1:
            await asyncio.sleep(interval)                 # 取消期间 _cancellable 中止 → 行 pending、resume 重轮
    raise ValueError(f"轮询 {attempts} 次仍未达完成状态 '{until}'（HTTP {method} {url}）")
```

然后把 `run_http_fetch_row`（原第 243-248 行的 fetch+parse 段）替换为调用 `_http_poll`。改后函数体为：

```python
async def run_http_fetch_row(config: dict, row: dict) -> tuple[list[dict], dict]:
    """处理一条输入行：渲染 endpoint/params/headers/body 后调接口（可轮询），按 extract 的 JSON 路径提取落列。
    返回 (输出行列表, 空 usage)。请求失败/响应非 JSON/轮询超时抛异常由 runner 记为行失败（逐行隔离）。
    params(含 api_key)合并进查询串；body_format 决定 Content-Type（用户已在 headers 设置则不覆盖）。"""
    base = strip_internal(row)
    method = config.get("method", "GET")
    endpoint = render_template(config.get("endpoint") or config.get("url", ""), base)
    params = {k: render_template(str(v), base) for k, v in (config.get("params") or {}).items()}
    url = str(httpx.URL(endpoint).copy_merge_params(params)) if params else endpoint
    headers = {k: render_template(str(v), base) for k, v in (config.get("headers") or {}).items()}
    body = render_template(config["body"], base) if config.get("body") else None
    ct = _CONTENT_TYPES.get(config.get("body_format"))
    if body and ct and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = ct
    data = await _http_poll(config, method, url, headers, body)
    extracted = {}
    for col, path in (config.get("extract") or {}).items():
        v = json_path_get(data, path)
        extracted[col] = "" if v is None else v   # 字段缺失→空串，非缺失保原类型
    return [{**base, **extracted}], {}
```

- [ ] **Step 4: 跑测试确认通过 + 回归现有 http 测试**

Run: `cd backend && python -m pytest tests/test_http_node.py -v`
Expected: PASS（3 个新测试 + 现有 11 个 http 测试全绿——非轮询路径逐字节不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/nodes.py backend/tests/test_http_node.py
git commit -m "feat(engine): http_fetch 支持轮询同一请求直到状态字段达期望值(间隔+次数上限)"
```

---

### Task 2: `run_http_fetch_row` 结果展开（`records_path`）

**Files:**
- Modify: `backend/app/engine/nodes.py`（加 `_http_extract` 助手；改 `run_http_fetch_row` 的提取段）
- Test: `backend/tests/test_http_node.py`（追加 4 个测试）

**Interfaces:**
- Consumes: Task 1 的 `_http_poll`、现有 `json_path_get`。
- Produces: 新增模块级 `def _http_extract(obj, extract: dict) -> dict`——对 obj 按 extract（列名→JSON 路径）取值，None→空串。`run_http_fetch_row` 行为扩展：配了 `records_path` 时取该路径数组、每元素一行（extract 相对元素）；非数组抛 ValueError（含 "records_path"）；空数组→`[]`。未配则单行（extract 相对整个响应）。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_http_node.py` 末尾追加：

```python
async def test_http_explode_records_to_rows(monkeypatch):
    """配了 records_path：数组每元素出一行，extract 相对元素取值，base 列并入每行。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"items": [{"name": "a", "age": 1}, {"name": "b", "age": 2}]})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://x", "records_path": "items", "extract": {"who": "name", "yr": "age"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"src": "S"})
    assert out == [{"src": "S", "who": "a", "yr": 1}, {"src": "S", "who": "b", "yr": 2}]


async def test_http_explode_empty_array_zero_rows(monkeypatch):
    """records_path 指向空数组：产 0 行（合法，本次取数无记录），不算失败。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"items": []})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://x", "records_path": "items", "extract": {"who": "name"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"src": "S"})
    assert out == []


async def test_http_explode_non_array_raises(monkeypatch):
    """records_path 未指向数组：抛 ValueError（含 records_path），由 runner 记为行/run 失败。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"items": {"not": "a list"}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://x", "records_path": "items", "extract": {}}
    with pytest.raises(ValueError, match="records_path"):
        await nodes.run_http_fetch_row(cfg, {})


async def test_http_poll_then_explode(monkeypatch):
    """轮询 + 展开组合：poll 到 done 后把数组展开成多行。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        if calls["n"] < 2:
            return 200, json.dumps({"status": "pending"})
        return 200, json.dumps({"status": "done", "rows": [{"x": 1}, {"x": 2}]})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://x", "poll_status_path": "status", "poll_until": "done",
           "poll_interval": 0, "poll_max_attempts": 5, "records_path": "rows", "extract": {"x": "x"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {})
    assert calls["n"] == 2
    assert out == [{"x": 1}, {"x": 2}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py -k "explode or poll_then" -v`
Expected: FAIL（`records_path` 未被识别，展开测试拿到 1 行含整数组；非数组测试不抛错）

- [ ] **Step 3: 实现 `_http_extract` 并改 `run_http_fetch_row` 提取段**

在 `backend/app/engine/nodes.py` 的 `json_path_get` 函数之后插入：

```python
def _http_extract(obj, extract: dict) -> dict:
    """按 extract（输出列名 → JSON 路径）从 obj 取值落列；缺失→空串，非缺失保原类型。"""
    out = {}
    for col, path in extract.items():
        v = json_path_get(obj, path)
        out[col] = "" if v is None else v
    return out
```

把 `run_http_fetch_row` 末尾（Task 1 改后的 `data = await _http_poll(...)` 之后的提取+return）替换为：

```python
    data = await _http_poll(config, method, url, headers, body)
    extract = config.get("extract") or {}
    records_path = config.get("records_path")
    if records_path:
        arr = json_path_get(data, records_path)
        if not isinstance(arr, list):
            raise ValueError(f"records_path '{records_path}' 未指向数组（实际 {type(arr).__name__}）")
        return [{**base, **_http_extract(el, extract)} for el in arr], {}
    return [{**base, **_http_extract(data, extract)}], {}
```

- [ ] **Step 4: 跑测试确认通过 + 回归整个 http 测试文件**

Run: `cd backend && python -m pytest tests/test_http_node.py -v`
Expected: PASS（含 Task 1、Task 2 新测试与原有 11 个；现有 `..._renders_and_extracts` 等单行用例仍走 `_http_extract(data, extract)` 分支不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/nodes.py backend/tests/test_http_node.py
git commit -m "feat(engine): http_fetch 配 records_path 把响应数组展开成多行(extract 相对元素)"
```

---

### Task 3: 新 config 键的脏草稿预校验

**Files:**
- Modify: `backend/app/engine/runner.py:420-434`（`validate_node_config_shape` 的 http_fetch 分支末尾追加）
- Test: `backend/tests/test_http_node.py`（扩 `test_http_node_dirty_config_fails_run_named` 的 parametrize）

**Interfaces:**
- Consumes: 现有 `validate_node_config_shape(node)` 在 `_run_http_node`/生成循环里被调用、不符抛 ValueError → `execute_run` 落 run.failed 点名。
- Produces: http_fetch 分支新增校验：`poll_status_path`/`records_path` present 时须为 str；`poll_interval` present 时须为 ≥0 数字（非 bool）；`poll_max_attempts` present 时须为 ≥1 整数（非 bool）；配了 `poll_status_path` 但缺 `poll_until` → 报错。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_http_node.py` 的 `test_http_node_dirty_config_fails_run_named` 上方的 `@pytest.mark.parametrize` 列表里追加这些用例（放在现有 `extract` 用例之后、`])` 之前）：

```python
    ({"endpoint": "http://x", "poll_status_path": ["bad"]}, "poll_status_path"),
    ({"endpoint": "http://x", "poll_status_path": "status"}, "poll_until"),  # 配了路径却没完成值
    ({"endpoint": "http://x", "poll_interval": -1}, "poll_interval"),
    ({"endpoint": "http://x", "poll_max_attempts": 0}, "poll_max_attempts"),
    ({"endpoint": "http://x", "records_path": {"bad": 1}}, "records_path"),
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py::test_http_node_dirty_config_fails_run_named -v`
Expected: FAIL（新增 5 个 parametrize 实例：run 误报 completed 或 error 不含被测键）

- [ ] **Step 3: 实现校验**

在 `backend/app/engine/runner.py` 的 `validate_node_config_shape` 内、http_fetch 分支的 `extract` 校验（第 433-434 行）之后追加：

```python
        psp = cfg.get("poll_status_path")
        if psp is not None and not isinstance(psp, str):
            raise ValueError(f"http_fetch 节点 {node.id}: poll_status_path 必须为字符串，当前为 {type(psp).__name__}")
        if psp and cfg.get("poll_until") is None:
            raise ValueError(f"http_fetch 节点 {node.id}: 配了 poll_status_path 就必须配 poll_until（完成状态值）")
        pi = cfg.get("poll_interval")
        if pi is not None and (isinstance(pi, bool) or not isinstance(pi, (int, float)) or pi < 0):
            raise ValueError(f"http_fetch 节点 {node.id}: poll_interval 必须为 ≥0 的数字，当前为 {pi!r}")
        pa = cfg.get("poll_max_attempts")
        if pa is not None and (isinstance(pa, bool) or not isinstance(pa, int) or pa < 1):
            raise ValueError(f"http_fetch 节点 {node.id}: poll_max_attempts 必须为 ≥1 的整数，当前为 {pa!r}")
        rp = cfg.get("records_path")
        if rp is not None and not isinstance(rp, str):
            raise ValueError(f"http_fetch 节点 {node.id}: records_path 必须为字符串，当前为 {type(rp).__name__}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_http_node.py::test_http_node_dirty_config_fails_run_named -v`
Expected: PASS（含原有 7 个 + 新增 5 个 parametrize 实例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_http_node.py
git commit -m "feat(engine): http_fetch 轮询/展开新键脏草稿预校验(整run failed点名节点)"
```

---

### Task 4: 无输入 http_fetch 改走 topo 作起始数据源

**Files:**
- Modify: `backend/app/engine/runner.py:205-210`（`_node_inputs` 加种子）
- Modify: `backend/app/engine/runner.py:472-484`（`_generation_chain` 的 `gen_types` 去 http + 文案）
- Modify: `backend/app/engine/runner.py:754-798`（`_run_generation_loop` 删 http 分支、简化 llm-only）
- Test: `backend/tests/test_http_node.py`（追加 2 个数据源测试）

**Interfaces:**
- Consumes: 现有 `attach_root_trace`（runner.py 已 import，第 788 行在用）、`upstream_ids`、topo 路径里的 `_run_http_node`→`_run_per_row_node`、output 的 `_run_barrier_node`（按 count 截断）。
- Produces: `_node_inputs` 对「无上游父节点且 type=='http_fetch'」的节点返回 `attach_root_trace([{}], run_id=..., node_id=...)`（一个带 root trace 的空种子）触发一次取数；其余无父节点仍返回 `[]`。`_generation_chain` 的 `gen_types` 收窄为 `{"llm_synth"}`，无输入 http 不再进生成循环。`_run_generation_loop` 起点恒为 llm_synth。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_http_node.py` 末尾追加：

```python
DATA_SOURCE_GRAPH = {
    "nodes": [
        {"id": "src", "type": "http_fetch",
         "config": {"url": "http://api/list", "records_path": "items", "extract": {"name": "name"}}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "src", "target": "out", "kind": "normal"}],
}


async def test_http_data_source_explodes_into_dataset(session_factory, monkeypatch):
    """无 input 节点：http_fetch 作起始数据源，触发一次取数→展开成多行→流到 output。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"items": [{"name": "x"}, {"name": "y"}, {"name": "z"}]})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=DATA_SOURCE_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["name"] for r in out} == {"x", "y", "z"}


async def test_http_data_source_single_object(session_factory, monkeypatch):
    """无 records_path 的起始 http_fetch：取一次单对象 → 单行。"""
    graph = {
        "nodes": [
            {"id": "src", "type": "http_fetch", "config": {"url": "http://api", "extract": {"v": "val"}}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "src", "target": "out", "kind": "normal"}],
    }

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"val": 7})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert out == [{"v": 7}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py -k data_source -v`
Expected: FAIL（当前无输入 http 进生成循环，且 output.count 缺失→`_run_generation_loop` 取 `output.config["count"]` KeyError → run failed；或 0 行。两测试均不得 completed+正确行）

- [ ] **Step 3a: `_node_inputs` 加种子**

把 `backend/app/engine/runner.py` 的 `_node_inputs`（第 205-210 行）替换为：

```python
async def _node_inputs(session_factory, run_id, graph: Graph, node: Node) -> list[dict]:
    parents = upstream_ids(graph, node.id)
    if not parents and node.type == "http_fetch":
        # 无上游的 http_fetch = 起始数据源：喂一个带 root trace 的空种子，触发一次取数（否则 0 行 no-op）
        return attach_root_trace([{}], run_id=run_id, node_id=node.id)
    branches = [await _node_outputs(session_factory, run_id, uid, include_trace=True) for uid in parents]
    if len(branches) <= 1:
        return branches[0] if branches else []
    return _merge_branches(node.id, branches)
```

- [ ] **Step 3b: `_generation_chain` 的 gen_types 去 http**

在 `backend/app/engine/runner.py` 的 `_generation_chain` 内，把第 475 行：

```python
    gen_types = {"llm_synth", "http_fetch"}
```

改为：

```python
    gen_types = {"llm_synth"}   # http_fetch 无输入时走 topo 作数据源（一次取数+展开），不进生成循环
```

并把该函数 docstring（第 474 行）里 `start(llm/http)` 改为 `start(llm_synth)`。

- [ ] **Step 3c: `_run_generation_loop` 简化为 llm-only**

在 `backend/app/engine/runner.py` 的 `_run_generation_loop` 内：

把第 762-768 行：

```python
    mc = None
    if start.type == "llm_synth":
        async with session_factory() as s:
            mc = await s.get(ModelConfig, start.config.get("model_config_id"))
        if mc is None or mc.user_id != user_id:
            raise ValueError(f"节点 {start.id}: 模型配置不存在")
    fanout = start.config.get("fanout_n", 1) if start.type == "llm_synth" else 1
```

替换为（起点恒为 llm_synth）：

```python
    async with session_factory() as s:
        mc = await s.get(ModelConfig, start.config.get("model_config_id"))
    if mc is None or mc.user_id != user_id:
        raise ValueError(f"节点 {start.id}: 模型配置不存在")
    fanout = start.config.get("fanout_n", 1)
```

把第 789-798 行：

```python
            if start.type == "llm_synth":
                await _run_per_row_node(
                    session_factory, run_id, user_id, start, seeds, cancel_event,
                    row_coro=lambda i, rs=seeds: nodes.run_llm_synth_row(start.config, rs[i], mc, user_sem),
                    log_source="synth", row_base=base, finalize_state=False)
            else:
                await _run_per_row_node(
                    session_factory, run_id, user_id, start, seeds, cancel_event,
                    row_coro=lambda i, rs=seeds: nodes.run_http_fetch_row(start.config, rs[i]),
                    row_base=base, finalize_state=False)
```

替换为：

```python
            await _run_per_row_node(
                session_factory, run_id, user_id, start, seeds, cancel_event,
                row_coro=lambda i, rs=seeds: nodes.run_llm_synth_row(start.config, rs[i], mc, user_sem),
                log_source="synth", row_base=base, finalize_state=False)
```

- [ ] **Step 4: 跑测试确认通过 + 关键回归**

Run: `cd backend && python -m pytest tests/test_http_node.py tests/test_gen_loop.py tests/test_runner.py tests/test_columns.py -v`
Expected: PASS（2 个数据源新测试绿；`test_gen_loop.py` llm 生成链不受影响；含 input 节点的 http 工作流 `test_http_node_fetches_each_row` 等仍绿——它们 http 节点有上游父节点，不触发种子分支）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_http_node.py
git commit -m "feat(engine): 无输入 http_fetch 改走 topo 作起始数据源(单种子触发一次取数+展开)"
```

---

### Task 5: 前端 HttpFetchForm 轮询面板 + records_path 字段

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx:896-917`（`HttpFetchForm` 的「提取」与「高级」面板间加「轮询」面板、提取面板加 records_path）
- Test: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`（追加轮询/展开字段渲染与 patch 断言）

**Interfaces:**
- Consumes: 现有 `Field`、`Input`、`InputNumber`、`Collapse` items 写法、`patch(p)`（`onChange({...config, ...p})`）、`config`。
- Produces: 轮询 Collapse 面板（key `'poll'`）含 4 个字段写 `poll_status_path`/`poll_until`/`poll_interval`/`poll_max_attempts`；提取面板加一个写 `records_path` 的 `Input`。无 Switch——presence-based（状态路径留空=不轮询）。

- [ ] **Step 1: 写失败测试**

先确认现有测试里 http 表单的渲染入口（查 `NodeConfigForm.test.tsx` 里已有的 http_fetch 用例的 render 写法），仿照它在 `frontend/src/canvas/forms/NodeConfigForm.test.tsx` 追加：

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import NodeConfigForm from './NodeConfigForm'

describe('HttpFetchForm 轮询与展开', () => {
  it('展示轮询字段，编辑写入 poll_status_path', () => {
    const onChange = vi.fn()
    render(<NodeConfigForm type="http_fetch" config={{ url: 'http://x' }} onChange={onChange} />)
    const input = screen.getByPlaceholderText('状态字段 JSON 路径 如 status')
    fireEvent.change(input, { target: { value: 'status' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ poll_status_path: 'status' }))
  })

  it('展示 records_path，编辑写入 records_path', () => {
    const onChange = vi.fn()
    render(<NodeConfigForm type="http_fetch" config={{ url: 'http://x' }} onChange={onChange} />)
    const input = screen.getByPlaceholderText('数组 JSON 路径 如 data.items（留空=不展开）')
    fireEvent.change(input, { target: { value: 'items' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ records_path: 'items' }))
  })
})
```

> 注：Ant Design `Collapse` 默认折叠时子项不挂载。若现有 http 测试已设 `defaultActiveKey`/全展开写法，沿用之；否则在本测试 render 后先点开对应面板标题（`fireEvent.click(screen.getByText(...))`）再查字段。实现 Step 3 时把轮询面板与提取面板默认展开或与现有面板一致即可。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx -t "轮询与展开"`
Expected: FAIL（找不到 placeholder 对应的输入框）

- [ ] **Step 3: 实现表单字段**

在 `frontend/src/canvas/forms/NodeConfigForm.tsx` 的 `HttpFetchForm` 的 Collapse `items` 数组里：

(a) 把「提取」面板（第 896-901 行 `{ key: 'extract', ... }`）替换为：

```tsx
        { key: 'extract', label: '提取', children: (
          <>
            <Field label="提取（响应 JSON 路径 → 输出列；如 temp ← data.temp）">
              <KvEditor pairs={config.extract ?? {}} onChange={(e) => patch({ extract: e })}
                        keyPlaceholder="输出列名" valPlaceholder="JSON 路径 如 data.weather.0.desc" />
            </Field>
            <Field label="展开数组成多行（可选）：填了即把该路径下的数组每个元素摊成一行，上方提取路径相对每个元素">
              <Input value={config.records_path ?? ''}
                     placeholder="数组 JSON 路径 如 data.items（留空=不展开）"
                     onChange={(e) => patch({ records_path: e.target.value || undefined })} />
            </Field>
          </>
        ) },
```

(b) 在「提取」面板与「高级」面板之间插入新的「轮询」面板：

```tsx
        { key: 'poll', label: '轮询（异步任务等待；留空状态路径=不轮询）', children: (
          <>
            <Field label="状态字段路径：反复发同一请求，直到此 JSON 路径的值达「完成值」">
              <Input value={config.poll_status_path ?? ''}
                     placeholder="状态字段 JSON 路径 如 status"
                     onChange={(e) => patch({ poll_status_path: e.target.value || undefined })} />
            </Field>
            <Field label="完成值（状态字段等于它即停止轮询）">
              <Input value={config.poll_until ?? ''}
                     placeholder="如 completed / done"
                     onChange={(e) => patch({ poll_until: e.target.value || undefined })} />
            </Field>
            <Space wrap>
              <Field label="轮询间隔(秒)"><InputNumber min={0} value={config.poll_interval ?? 2}
                onChange={(v) => patch({ poll_interval: v ?? 2 })} /></Field>
              <Field label="轮询次数上限"><InputNumber min={1} value={config.poll_max_attempts ?? 30}
                onChange={(v) => patch({ poll_max_attempts: v ?? 30 })} /></Field>
            </Space>
          </>
        ) },
```

(c) 把「高级」面板（第 902 行）的 label 与重试字段补一句区分注释——把重试 `Field` 改为：

```tsx
              <Field label="重试次数（单次请求传输层失败重试，与轮询次数不同）"><InputNumber min={0} value={config.retries ?? 2}
                onChange={(v) => patch({ retries: v ?? 2 })} /></Field>
```

- [ ] **Step 4: 跑测试确认通过 + 类型检查 + 前端全量**

Run: `cd frontend && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx && npx tsc --noEmit`
Expected: PASS（轮询/展开新测试绿；现有 NodeConfigForm 测试绿；tsc 无错）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx
git commit -m "feat(web): http_fetch 表单加轮询面板(状态路径/完成值/间隔/次数)+展开 records_path 字段"
```

---

### Task 6: 全量回归 + spec 留观记录

**Files:**
- Modify: `docs/superpowers/specs/2026-06-25-http-fetch-polling-explode-design.md`（实现后补「实现记录/留观」段，如有偏差）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest -q`
Expected: PASS（全绿，新增 ~14 个 http 测试）。若有失败，定位修复后重跑。

- [ ] **Step 2: 前端全量测试 + 类型检查**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: PASS（全绿，tsc 无错）

- [ ] **Step 3: 补 spec 留观（仅当实现与设计有偏差或发现已知限制）**

如实现完全照设计，本步在 spec 末尾追加一行「实现记录：2026-06-25 按计划实现，N 个任务，后端/前端全绿」即可；如有偏差或新发现的已知限制（如起始节点进度显示 1/N），据实补「已知限制」。

- [ ] **Step 4: 提交**

```bash
git add docs/superpowers/specs/2026-06-25-http-fetch-polling-explode-design.md
git commit -m "docs(spec): http_fetch 轮询+展开+起始数据源 实现记录"
```

---

## Self-Review

**Spec coverage（逐节核对）：**
- §1 新增 config 键 → Task 1（poll_*）、Task 2（records_path），UI 在 Task 5。✓
- §2 轮询循环 → Task 1（`_http_poll`，第 1 次立即发/sleep/次数上限/非 JSON 当未就绪/同步路径不变）。✓
- §3 结果展开 → Task 2（records_path 非数组报错、空数组 0 行、extract 相对元素）。✓
- §4 起始数据源路由 → Task 4（gen_types 去 http、_node_inputs 种子、_run_generation_loop 简化）。✓
- §5 config 预校验 → Task 3。✓
- §6 前端 → Task 5（轮询面板 + records_path + retries 区分注释）。✓
- 边界/取舍（单 RunRow、retries vs poll_max_attempts、完成值 str 比较）→ 体现在实现与注释；起始进度 1/N 留观在 Task 6。✓
- 测试计划 11 项 → 分摊到 Task 1（3）/Task 2（4）/Task 3（5 parametrize）/Task 4（2 + 回归）/Task 5（前端 2）/Task 6（全量）。✓
- 列血缘不变 → 无 columns.py 改动，Task 4 Step 4 跑 test_columns.py 回归。✓

**Placeholder 扫描：** 每个 code step 均含完整可粘贴代码与确切命令/预期；无 TBD/TODO/“类似 Task N”。✓

**类型一致性：** `_http_poll(config, method, url, headers, body)` 在 Task 1 定义、Task 2 调用一致；`_http_extract(obj, extract)` Task 2 定义并在单行/展开两路调用；`run_http_fetch_row(config,row)->(list,dict)` 全程签名不变；`attach_root_trace([{}], run_id=, node_id=)` 与 trace.py 签名一致；config 键名 `poll_status_path/poll_until/poll_interval/poll_max_attempts/records_path` 在校验、引擎、前端三处拼写一致。✓
