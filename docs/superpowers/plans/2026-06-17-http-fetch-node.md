# HTTP 取数节点 + 列可见性 UX 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增逐行 `http_fetch` 节点（按行调外部接口、JSON 路径提取落列），并在配置面板绿标 LLM/节点实际用到的列、列多时改下拉框展示全部。

**Architecture:** 照 `llm_synth` 的逐行隔离模式新增 `_run_http_node` handler + `run_http_fetch_row` worker；新建 `app/services/http.py`（仿 `llm.py` 的缓存/重试/错误类型）；列血缘 `columns.py` 加 `http_fetch` 分支；前端注册新节点类型 + `HttpFetchForm`，`ColumnsBar` 复用既有 `TPL_RE` 做绿标、列多时换 antd `Select`。

**Tech Stack:** FastAPI + SQLAlchemy 2 async（SQLite，无 migration）、httpx、React 19 + AntD 6 + React Flow + Vite。

**贯穿约束（KISS 硬规则）：** 最简实现、不预防未发生的 bug；HTTP token 明文存 `graph_json` 是知情取舍，但 **error/日志/SSE 一律不含 headers 值**；`http_fetch` 不引用租户资源，不需要 `create_run` 校验分支。

**测试运行环境：** 后端在 `backend/` 目录执行 `python -m pytest ...`（`testpaths=["tests"]`，`tools/` 不会被收集）；前端在 `frontend/` 目录执行 `npm run build`（`tsc -b && vite build`，类型检查即验证）。

---

### Task 1: `json_path_get` 纯函数

**Files:**
- Modify: `backend/app/engine/nodes.py`（在 `render_template`/`_cell` 之后，约第 128 行后追加）
- Test: `backend/tests/test_http_node.py`（新建）

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_http_node.py`：

```python
from app.engine import nodes


def test_json_path_get_dotted_and_index():
    obj = {"data": {"temp": 25, "weather": [{"desc": "晴"}, {"desc": "雨"}]}}
    assert nodes.json_path_get(obj, "data.temp") == 25
    assert nodes.json_path_get(obj, "data.weather.0.desc") == "晴"
    assert nodes.json_path_get(obj, "data.weather.1.desc") == "雨"


def test_json_path_get_missing_returns_none():
    obj = {"data": {"temp": 25}}
    assert nodes.json_path_get(obj, "data.humidity") is None      # 缺键
    assert nodes.json_path_get(obj, "data.weather.0") is None      # 在非 list/dict 上下钻
    assert nodes.json_path_get(obj, "data.temp.x") is None         # 在 int 上下钻
    assert nodes.json_path_get(obj, "data.list.5") is None         # 索引越界（且 list 不存在）


def test_json_path_get_negative_and_root():
    assert nodes.json_path_get([10, 20, 30], "-1") == 30
    assert nodes.json_path_get({"a": 1}, "a") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: FAIL — `AttributeError: module 'app.engine.nodes' has no attribute 'json_path_get'`

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/engine/nodes.py` 的 `_cell` 函数（约第 126-127 行）之后追加：

```python
def json_path_get(obj, path: str):
    """点号路径取值：data.weather.0.desc —— 数字段对 list 当索引、对 dict 当键。
    任一级类型不符或缺失返回 None（落列时再归一成空串）。不支持通配/过滤（YAGNI）。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                return None
            idx = int(part)
            if not -len(cur) <= idx < len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/nodes.py backend/tests/test_http_node.py
git commit -m "feat(engine): json_path_get 点号/索引路径取值" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `app/services/http.py` 取数服务

**Files:**
- Create: `backend/app/services/http.py`
- Test: `backend/tests/test_http_service.py`（新建）

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_http_service.py`：

```python
import httpx
import pytest

from app.services import http


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(http, "BACKOFF_BASE", 0)
    http._client_cache.clear()
    yield
    http._client_cache.clear()


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_status_and_text(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text='{"ok":1}')
    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    status, text = await http.fetch("GET", "http://x/api")
    assert status == 200 and text == '{"ok":1}'


async def test_fetch_post_sends_body_and_headers(monkeypatch):
    seen = {}
    def handler(request):
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="ok")
    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    await http.fetch("POST", "http://x/api", headers={"Authorization": "Bearer T"}, body='{"q":1}')
    assert seen["body"] == '{"q":1}' and seen["auth"] == "Bearer T"


async def test_fetch_4xx_raises_without_leaking_headers(monkeypatch):
    def handler(request):
        return httpx.Response(403, text="forbidden")
    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    with pytest.raises(http.HTTPFetchError) as e:
        await http.fetch("GET", "http://x/api", headers={"Authorization": "Bearer SECRET"}, retries=2)
    msg = str(e.value)
    assert "403" in msg and "SECRET" not in msg and "Authorization" not in msg


async def test_fetch_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] >= 2 else 500, text="late-ok")
    monkeypatch.setitem(http._client_cache, "c", _mock_client(handler))
    status, text = await http.fetch("GET", "http://x/api", retries=3)
    assert status == 200 and calls["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_http_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.http'`

- [ ] **Step 3: Write minimal implementation**

新建 `backend/app/services/http.py`：

```python
"""通用 HTTP 取数：httpx 客户端复用 + 显式重试/退避 + 统一错误类型。仿 app/services/llm.py。
错误文案只含 method/url/status，绝不含 headers（防 token 外泄）。"""
import asyncio

import httpx

BACKOFF_BASE = 1  # 秒；重试等待 BACKOFF_BASE * 2**attempt，测试中置 0

_client_cache: dict[str, httpx.AsyncClient] = {}


class HTTPFetchError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    """复用单个 AsyncClient（连接池），避免每行重建。"""
    if "c" not in _client_cache:
        _client_cache["c"] = httpx.AsyncClient()
    return _client_cache["c"]


async def fetch(method: str, url: str, headers: dict | None = None, body: str | None = None,
                timeout: int = 30, retries: int = 2) -> tuple[int, str]:
    """发一次请求，返回 (status, text)。非 2xx/网络错重试 retries 次仍失败抛 HTTPFetchError。"""
    client = _client()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.request(method, url, headers=headers or None,
                                        content=body if body else None, timeout=timeout)
            if resp.status_code >= 400:
                raise HTTPFetchError(f"HTTP {resp.status_code} {method} {url}")  # 不含 headers
            return resp.status_code, resp.text
        except HTTPFetchError as e:
            last_err = e
        except Exception as e:
            last_err = HTTPFetchError(f"请求失败 {method} {url}: {e}")  # 不含 headers
        if attempt < retries - 1:
            await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    raise last_err if last_err else HTTPFetchError(f"请求失败 {method} {url}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_http_service.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/http.py backend/tests/test_http_service.py
git commit -m "feat(services): http 取数服务(缓存/重试/HTTPFetchError，err 不含 headers)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `run_http_fetch_row` 逐行 worker

**Files:**
- Modify: `backend/app/engine/nodes.py`（顶部 import 加 `http`；在 `run_llm_synth_row` 之后追加 worker）
- Test: `backend/tests/test_http_node.py`（追加）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_http_node.py` 末尾追加：

```python
import json


async def test_run_http_fetch_row_renders_and_extracts(monkeypatch):
    seen = {}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(method=method, url=url, headers=headers)
        return 200, json.dumps({"data": {"temp": 25, "weather": [{"desc": "晴"}]}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "GET", "url": "http://api/{{city}}",
           "headers": {"X-City": "{{city}}"},
           "extract": {"temp": "data.temp", "desc": "data.weather.0.desc"}}
    out, usage = await nodes.run_http_fetch_row(cfg, {"city": "北京"})
    assert seen["url"] == "http://api/北京"          # url 模板渲染
    assert seen["headers"]["X-City"] == "北京"        # header 值模板渲染
    assert out == [{"city": "北京", "temp": 25, "desc": "晴"}]  # 保原类型，并入行
    assert usage == {}                                # 无 token


async def test_run_http_fetch_row_missing_field_becomes_empty(monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"data": {"temp": 25}})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://api", "extract": {"temp": "data.temp", "missing": "data.nope"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert out == [{"id": "1", "temp": 25, "missing": ""}]  # 字段缺失→空串，不算失败


async def test_run_http_fetch_row_non_json_raises(monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, "<html>not json</html>"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    with pytest.raises(ValueError):
        await nodes.run_http_fetch_row({"url": "http://api", "extract": {"x": "a"}}, {"id": "1"})
```

`test_http_node.py` 顶部需要 `import pytest`（追加到现有 import）。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: FAIL — `AttributeError: module 'app.engine.nodes' has no attribute 'run_http_fetch_row'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/engine/nodes.py` 顶部 import（第 9 行 `from app.services import llm` 改为同时引入 http）：

```python
from app.services import http, llm
```

在 `run_llm_synth_row`（约第 175 行结束）之后追加：

```python
async def run_http_fetch_row(config: dict, row: dict) -> tuple[list[dict], dict]:
    """处理一条输入行：渲染 url/headers/body 后调接口，按 extract 的 JSON 路径提取落列。
    返回 (输出行列表, 空 usage)。请求失败/响应非 JSON 抛异常由 runner 记为行失败（逐行隔离）。"""
    base = strip_qc_internal(row)
    method = config.get("method", "GET")
    url = render_template(config.get("url", ""), base)
    headers = {k: render_template(str(v), base) for k, v in (config.get("headers") or {}).items()}
    body = render_template(config["body"], base) if config.get("body") else None
    status, text = await http.fetch(method, url, headers=headers, body=body,
                                    timeout=config.get("timeout", 30), retries=config.get("retries", 2))
    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        raise ValueError(f"接口响应非 JSON，无法提取（HTTP {status} {url}）")
    extracted = {}
    for col, path in (config.get("extract") or {}).items():
        v = json_path_get(data, path)
        extracted[col] = "" if v is None else v   # 字段缺失→空串（同 render 缺失语义），非缺失保原类型
    return [{**base, **extracted}], {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/nodes.py backend/tests/test_http_node.py
git commit -m "feat(engine): run_http_fetch_row 逐行取数 worker(渲染/提取/缺失空串/非JSON报错)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 注册节点类型 + 列血缘

**Files:**
- Modify: `backend/app/engine/graph.py:4`（`NODE_TYPES`）
- Modify: `backend/app/engine/columns.py`（`_node_output`，约第 44-59 行）
- Test: `backend/tests/test_columns.py`（追加）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_columns.py` 末尾追加：

```python
def test_http_fetch_adds_extract_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "h", "type": "http_fetch",
          "config": {"extract": {"temp": "data.temp", "desc": "data.weather.0.desc"}}}],
        [{"source": "in", "target": "h", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"]})
    assert cols["h"]["output"] == ["id", "q", "temp", "desc"]


def test_http_fetch_no_extract_passthrough():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "h", "type": "http_fetch", "config": {"url": "http://x"}}],
        [{"source": "in", "target": "h", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"]})
    assert cols["h"]["output"] == ["id", "q"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_columns.py -v`
Expected: FAIL — `test_http_fetch_adds_extract_columns` 得 `["id","q"]`（`_node_output` 走默认透传分支），断言不等。

- [ ] **Step 3: Write minimal implementation**

`backend/app/engine/graph.py` 第 4 行：

```python
NODE_TYPES = {"input", "llm_synth", "auto_process", "output", "qc", "http_fetch"}
```

`backend/app/engine/columns.py` 的 `_node_output`，在 `auto_process` 分支（约第 54-58 行）之后、`return input_cols` 之前插入：

```python
    if t == "http_fetch":
        return _ordered_union([input_cols, list((node.config.get("extract") or {}).keys())])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_columns.py -v`
Expected: PASS（含原有 + 2 新）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/graph.py backend/app/engine/columns.py backend/tests/test_columns.py
git commit -m "feat(engine): 注册 http_fetch 节点类型 + 列血缘 output=input∪extract.keys" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `_run_http_node` handler + 派发（端到端）

**Files:**
- Modify: `backend/app/engine/runner.py`（`_execute` 派发约第 84-91 行；在 `_run_llm_node` 之后新增 handler）
- Test: `backend/tests/test_http_node.py`（追加端到端测试）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_http_node.py` 末尾追加（依赖 `test_runner.py` 的公共 helper，同目录直接 import）：

```python
from sqlalchemy import select

from app.engine import runner
from app.models import RunRow, RunNodeState
from test_runner import make_run, run_it, get_run

HTTP_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "fetch", "type": "http_fetch",
         "config": {"method": "GET", "url": "http://api/{{q}}",
                    "extract": {"echo": "data.echo"}, "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "fetch", "kind": "normal"},
              {"source": "fetch", "target": "out", "kind": "normal"}],
}


async def test_http_node_fetches_each_row(session_factory, monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        q = url.rsplit("/", 1)[-1]
        return 200, json.dumps({"data": {"echo": f"E{q}"}})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["echo"] for r in out} == {"E问0", "E问1", "E问2"}
    assert all("q" in r and "echo" in r for r in out)
    assert json.loads(run.stats_json) == {"prompt_tokens": 0, "completion_tokens": 0}  # http 无 token


async def test_http_node_row_failure_isolated(session_factory, monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        if "问1" in url:
            from app.services.http import HTTPFetchError
            raise HTTPFetchError("HTTP 500 GET " + url)
        return 200, json.dumps({"data": {"echo": "ok"}})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"                       # 单行失败不挂整 run
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out) == 2
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "fetch", RunRow.status == "failed"))).scalar_one()
    assert rec.row_idx == 1


async def test_http_node_resume_skips_done(session_factory, monkeypatch):
    calls = []

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls.append(url)
        return 200, json.dumps({"data": {"echo": "x"}})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    async with session_factory() as s:   # 预置 idx0 已完成
        s.add(RunRow(run_id=run_id, node_id="fetch", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "问0", "echo": "旧"}], ensure_ascii=False)))
        await s.commit()
    await run_it(session_factory, run_id)
    assert len(calls) == 2                                 # 只跑未完成的两行
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: FAIL — `http_fetch` 走 `else → _run_barrier_node → _barrier_output`，命中 `raise ValueError("未知节点类型: http_fetch")`，run.status == "failed"，断言不通过。

- [ ] **Step 3: Write minimal implementation**

`backend/app/engine/runner.py` 的 `_execute`，把派发分支（第 84-91 行）改为：

```python
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event)
        elif node.type == "qc":
            await _run_qc_node(session_factory, run_id, user_id, graph, node, inputs,
                               user_sem, cancel_event)
        elif node.type == "http_fetch":
            await _run_http_node(session_factory, run_id, user_id, node, inputs, cancel_event)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
```

在 `_run_llm_node`（第 276 行结束）之后新增 handler（照 `_run_llm_node`，去掉模型与 user_sem，worker 不占 LLM 全局并发槽）：

```python
async def _run_http_node(session_factory, run_id, user_id, node: Node, inputs, cancel_event):
    cfg = node.config
    async with session_factory() as s:
        existing = (await s.execute(select(RunRow.row_idx, RunRow.status).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id))).all()
    done_idx = {idx for idx, st in existing if st == "done"}
    failed_idx = {idx for idx, st in existing if st == "failed"}
    total = len(inputs)
    done_count, failed_count = len(done_idx), len(failed_idx)
    await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                          total=total, done=done_count, failed=failed_count)
    todo = [i for i in range(total) if i not in done_idx and i not in failed_idx]
    node_sem = asyncio.Semaphore(cfg.get("concurrency", 4))

    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await _cancellable(
                    nodes.run_http_fetch_row(cfg, inputs[idx]), cancel_event)
            except asyncio.CancelledError:
                return  # 硬中断：该行不落库（保持 pending）
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                failed_count += 1
            else:
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "",
                                  usage=usage)
                done_count += 1
            await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
    if not cancel_event.is_set():
        await _set_node_state(session_factory, run_id, node.id, user_id=user_id, status="done",
                              total=total, done=done_count, failed=failed_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_http_node.py -v`
Expected: PASS（9 passed）

然后跑全套确认无回归：

Run: `python -m pytest`
Expected: 全绿（既有数量 + 本批新增）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/runner.py backend/tests/test_http_node.py
git commit -m "feat(engine): _run_http_node 逐行 handler + 派发(隔离/并发/续跑，端到端跑通)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: gf CLI 支持 http_fetch

**Files:**
- Modify: `backend/app/cli.py`（`NODE_TYPES`、`NODE_LABELS`、`summarize`、`cmd_node_set`）
- Test: `backend/tests/test_cli.py`（追加）

CLI 支持：`add http`、`set url/method/body`（字符串）、`set extract=列:路径,列:路径`、`set conc/retries`（复用既有键，落 config 顶层，对 http 正确）。`headers`/`timeout` 走前端配（KISS 边界，CLI 不做嵌套 header 编辑）。

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_cli.py` 末尾追加：

```python
def test_http_node_add_set_show(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    capsys.readouterr()
    gf("node", "set", "http_fetch_1", "url=http://api/{{q}}", "method=GET",
       "extract=temp:data.temp,desc:data.weather.0.desc", "conc=8")
    capsys.readouterr()
    gf("node", "show", "http_fetch_1")
    node = json.loads(capsys.readouterr().out)
    assert node["type"] == "http_fetch"
    assert node["config"]["url"] == "http://api/{{q}}"
    assert node["config"]["method"] == "GET"
    assert node["config"]["extract"] == {"temp": "data.temp", "desc": "data.weather.0.desc"}
    assert node["config"]["concurrency"] == 8


def test_http_node_show_summary(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    gf("node", "set", "http_fetch_1", "url=http://api/x")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "HTTP 取数" in out and "http://api/x" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v -k http`
Expected: FAIL — `gf node add http` 报 `未知节点类型 http`（`NODE_TYPES` 无 http 别名）。

- [ ] **Step 3: Write minimal implementation**

`backend/app/cli.py` 第 12-15 行 `NODE_TYPES`/`NODE_LABELS` 改为：

```python
NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "qc": "qc", "llm_synth": "llm_synth", "auto_process": "auto_process",
              "http": "http_fetch", "http_fetch": "http_fetch"}
NODE_LABELS = {"input": "输入", "llm_synth": "LLM 合成", "auto_process": "自动处理",
               "output": "输出", "qc": "质检", "http_fetch": "HTTP 取数"}
```

`cmd_node_add` 的报错提示（第 202 行）顺带补 http（非必须，提示更准）：

```python
        die(f"未知节点类型 {args.type}（可选: input/llm/auto/output/qc/http）")
```

`summarize`（第 137-145 行），在 `auto_process` 分支后、最后的 return 前插入：

```python
    if n["type"] == "http_fetch":
        return f"{c.get('method', 'GET')} {c.get('url', '?')} -> {list((c.get('extract') or {}).keys())}"
```

`cmd_node_set` 的键处理 elif 链（第 226-245 行），在 `elif k in LLM_CONFIG_KEYS:` 之前插入 http 专用键：

```python
        elif k in HTTP_STR_KEYS:
            cfg[k] = v
        elif k == "extract":
            cfg["extract"] = dict(p.split(":", 1) for p in v.split(",") if ":" in p)
```

并在第 168 行附近（`FLOAT_KEYS` 之后）新增常量：

```python
HTTP_STR_KEYS = {"url", "method", "body"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v -k http`
Expected: PASS（2 passed）

确认 CLI 全套无回归：

Run: `python -m pytest tests/test_cli.py -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat(cli): gf 支持 http_fetch(add/set url·method·body·extract/show)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 前端注册 http_fetch 类型 + HttpFetchForm

**Files:**
- Modify: `frontend/src/api/types.ts:22`（`GraphNode['type']` union）
- Modify: `frontend/src/canvas/serialize.ts:4-10`（`NODE_LABELS`）
- Modify: `frontend/src/canvas/nodeTypes.tsx`（`COLORS`、`nodeTypes`）
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（新增 `KvEditor`、`HttpFetchForm`、`liveOutput` 分支、`switch` case）

- [ ] **Step 1: 加类型 union 与节点注册**

`frontend/src/api/types.ts` 第 22 行：

```typescript
  id: string; type: 'input' | 'llm_synth' | 'auto_process' | 'output' | 'qc' | 'http_fetch'
```

`frontend/src/canvas/serialize.ts` 第 4-10 行 `NODE_LABELS` 加一项：

```typescript
export const NODE_LABELS: Record<GraphNode['type'], string> = {
  input: '输入',
  llm_synth: 'LLM 合成',
  auto_process: '自动处理',
  output: '输出',
  qc: '质检',
  http_fetch: 'HTTP 取数',
}
```

`frontend/src/canvas/nodeTypes.tsx`：`COLORS`（第 4-6 行）加颜色、`nodeTypes`（第 28-30 行）注册渲染器：

```typescript
const COLORS: Record<string, string> = {
  input: '#1677ff', llm_synth: '#722ed1', auto_process: '#13c2c2', output: '#52c41a',
  qc: '#fa8c16', http_fetch: '#eb2f96',
}
```
```typescript
export const nodeTypes = {
  input: GFNode, llm_synth: GFNode, auto_process: GFNode, output: GFNode, qc: GFNode,
  http_fetch: GFNode,
}
```

- [ ] **Step 2: 新增 `KvEditor` + `HttpFetchForm` + liveOutput 分支 + switch case**

`frontend/src/canvas/forms/NodeConfigForm.tsx`：

在 `liveOutput`（第 41-57 行）的 `auto_process` 分支之后、`return inputCols` 之前插入：

```typescript
  if (type === 'http_fetch') return uniq([...inputCols, ...Object.keys(config.extract ?? {})])
```

在 `OutputNodeForm`（第 478 行结束）之后新增两个组件：

```tsx
function KvEditor({ pairs, onChange, keyPlaceholder, valPlaceholder }: {
  pairs: Record<string, string>; onChange: (p: Record<string, string>) => void
  keyPlaceholder: string; valPlaceholder: string
}) {
  const entries = Object.entries(pairs)
  const setEntry = (i: number, k: string, v: string) => {
    const next: Record<string, string> = {}
    entries.forEach(([ek, ev], j) => {
      const [nk, nv] = j === i ? [k, v] : [ek, ev]
      if (nk) next[nk] = nv
    })
    onChange(next)
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {entries.map(([k, v], i) => (
        <Space key={i}>
          <Input placeholder={keyPlaceholder} style={{ width: 150 }} value={k}
                 onChange={(e) => setEntry(i, e.target.value, v)} />
          <Input placeholder={valPlaceholder} style={{ width: 220 }} value={v}
                 onChange={(e) => setEntry(i, k, e.target.value)} />
          <a onClick={() => onChange(Object.fromEntries(entries.filter((_, j) => j !== i)))}>删除</a>
        </Space>
      ))}
      <Button size="small" onClick={() => onChange({ ...pairs, '': '' })}>+ 添加</Button>
    </Space>
  )
}

function HttpFetchForm({ config, onChange, inputCols }: FormProps & { inputCols: string[] }) {
  const patch = (p: object) => onChange({ ...config, ...p })
  return (
    <>
      <Field label="请求方法">
        <Radio.Group value={config.method ?? 'GET'} onChange={(e) => patch({ method: e.target.value })}>
          <Radio.Button value="GET">GET</Radio.Button>
          <Radio.Button value="POST">POST</Radio.Button>
        </Radio.Group>
      </Field>
      <Field label="URL（用 {{列名}} 引用上游数据列）">
        <Input.TextArea rows={2} value={config.url ?? ''}
                        onChange={(e) => patch({ url: e.target.value })} />
        <MissingColsWarning text={config.url ?? ''} inputCols={inputCols} />
      </Field>
      {(config.method ?? 'GET') === 'POST' && (
        <Field label="请求体 Body（{{列名}} 可引用；JSON 字符串）">
          <Input.TextArea rows={3} value={config.body ?? ''}
                          onChange={(e) => patch({ body: e.target.value })} />
          <MissingColsWarning text={config.body ?? ''} inputCols={inputCols} />
        </Field>
      )}
      <Field label="请求头 Headers（值可用 {{列名}}；如 Authorization / Bearer xxx）">
        <KvEditor pairs={config.headers ?? {}} onChange={(h) => patch({ headers: h })}
                  keyPlaceholder="Header 名" valPlaceholder="值" />
      </Field>
      <Field label="提取（响应 JSON 路径 → 输出列；如 temp ← data.temp）">
        <KvEditor pairs={config.extract ?? {}} onChange={(e) => patch({ extract: e })}
                  keyPlaceholder="输出列名" valPlaceholder="JSON 路径 如 data.weather.0.desc" />
      </Field>
      <Space wrap>
        <Field label="节点并发"><InputNumber min={1} value={config.concurrency ?? 4}
          onChange={(v) => patch({ concurrency: v ?? 4 })} /></Field>
        <Field label="重试次数"><InputNumber min={0} value={config.retries ?? 2}
          onChange={(v) => patch({ retries: v ?? 2 })} /></Field>
        <Field label="超时(秒)"><InputNumber min={1} value={config.timeout ?? 30}
          onChange={(v) => patch({ timeout: v ?? 30 })} /></Field>
      </Space>
    </>
  )
}
```

在默认导出的 `switch (type)`（第 498-511 行）中、`case 'output'` 之前加：

```tsx
    case 'http_fetch':
      return <>{bar}<HttpFetchForm config={config} onChange={onChange} inputCols={inputCols} /></>
```

（注：`outputCols`/`bar`/`canInsert` 的 http_fetch 适配在 Task 8 一并完成；本任务先让节点能渲染配置，`outputCols` 暂走 `nodeCols.output`，`bar` 的 onInsert 暂不针对 http。build 通过即可。）

- [ ] **Step 3: 验证构建通过**

Run（在 `frontend/`）: `npm run build`
Expected: tsc 类型检查 + vite 构建均无错误。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/canvas/serialize.ts frontend/src/canvas/nodeTypes.tsx frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(web): 注册 http_fetch 节点类型 + HttpFetchForm(method/url/body/headers/extract)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: ColumnsBar 绿标 + 列多下拉框

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`referencedCols` 助手、`ColumnsBar`、默认导出顶层的 `referenced`/`insertField`/`canInsert`/`outputCols`）

- [ ] **Step 1: 加 referencedCols 助手**

在 `missingCols`（第 21-27 行）之后插入：

```typescript
function referencedCols(text: string, inputCols: string[]): string[] {
  const out: string[] = []
  for (const m of (text ?? '').matchAll(TPL_RE)) {
    if (inputCols.includes(m[1]) && !out.includes(m[1])) out.push(m[1])
  }
  return out
}

const MANY_COLS = 12
```

- [ ] **Step 2: 改造 ColumnsBar（绿标 + 下拉框）**

把 `ColumnsBar`（第 59-79 行）整体替换为：

```tsx
function ColumnsBar({ inputCols, outputCols, referenced = [], onInsert }: {
  inputCols: string[]; outputCols: string[]; referenced?: string[]; onInsert?: (col: string) => void
}) {
  const refSet = new Set(referenced)
  const many = inputCols.length > MANY_COLS
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ color: '#666', marginBottom: 4 }}>
        输入列：{inputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
          : many
            ? <Select showSearch style={{ width: '100%', marginTop: 4 }} value={null} allowClear={false}
                      listHeight={320} placeholder={`全部 ${inputCols.length} 列，点选插入 {{列}}`}
                      onChange={(c) => c && onInsert?.(c as string)}
                      options={inputCols.map((c) => ({ value: c, label: refSet.has(c) ? `🟢 ${c}` : c }))} />
            : inputCols.map((c) => (
              <Tag key={c} color={refSet.has(c) ? 'green' : undefined}
                   style={{ cursor: onInsert ? 'pointer' : 'default', marginInlineEnd: 4 }}
                   onClick={() => onInsert?.(c)}>{c}</Tag>))}
      </div>
      <div style={{ color: '#666' }}>
        输出列：{outputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无）</span>
          : outputCols.map((c) => <Tag key={c} color="blue" style={{ marginInlineEnd: 4 }}>{c}</Tag>)}
      </div>
      {onInsert && <div style={{ color: '#999', marginTop: 4 }}>
        点输入列（<span style={{ color: '#52c41a' }}>绿色</span>=已被引用）插入 {'{{列}}'}{many ? '；列多已折叠为可搜索下拉框，展示全部' : ''}
      </div>}
    </div>
  )
}
```

- [ ] **Step 3: 默认导出顶层接线（referenced / http_fetch 插入目标 / outputCols）**

把默认导出 `NodeConfigForm`（第 480-497 行，从 `const nodeCols` 到 `bar` 定义）替换为：

```tsx
  const nodeCols = (nodeId && colsMap[nodeId]) || { input: [], output: [] }
  const inputCols = nodeCols.input
  const outputCols = type === 'llm_synth' || type === 'auto_process' || type === 'http_fetch'
    ? liveOutput(type, config, inputCols) : nodeCols.output
  // 绿标：本节点模板字段里 {{列}} 引用到、且确实在输入列中的列 = 实际用到的列
  const refText = type === 'http_fetch'
    ? [config.url, config.body, ...Object.values(config.headers ?? {})].filter(Boolean).join('\n')
    : `${config.system_prompt ?? ''}\n${config.user_prompt ?? ''}`
  const referenced = referencedCols(refText, inputCols)
  const insertField = type === 'http_fetch' ? 'url' : 'user_prompt'
  const canInsert = type === 'llm_synth' || type === 'qc' || type === 'http_fetch'
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols} referenced={referenced}
                onInsert={canInsert
                  ? (c) => onChange({ ...config, [insertField]: (config[insertField] ?? '') + `{{${c}}}` })
                  : undefined} />
  )
```

（`Object.values(config.headers ?? {})` 元素是 `unknown`，`filter(Boolean)` 后 `join` 接受；若 tsc 报类型，改 `[config.url, config.body, ...Object.values<string>(config.headers ?? {})]` 或 `.map(String)`。）

- [ ] **Step 4: 验证构建通过**

Run（在 `frontend/`）: `npm run build`
Expected: tsc + vite 构建无错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "feat(web): ColumnsBar 绿标已引用列 + 列多换可搜索下拉框(展示全部不省略)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾验证（全部任务完成后）

- [ ] 后端全套：在 `backend/` 跑 `python -m pytest`，应全绿（既有 + 本批新增 ~13 个用例）。
- [ ] 前端：在 `frontend/` 跑 `npm run build` 通过。
- [ ] 用 finishing-a-development-branch 收尾：测试全绿 → ff-merge 到 master → 删除 `feat/http-fetch-node` 分支（仅留 master）。

## 自查（plan 对 spec 覆盖核对）

- ✓ 加新节点 `http_fetch`（逐行 v1）：Task 4（注册）+ Task 5（handler/派发）。
- ✓ config：method/url/headers/body/extract/concurrency/retries/timeout：Task 3（worker 读）+ Task 7（前端编辑）+ Task 6（CLI 部分）。
- ✓ `json_path_get`：Task 1。
- ✓ 错误三态（请求失败→failed / 非 JSON→failed / 字段缺失→空串）：Task 3（worker 单测）+ Task 5（端到端隔离测试）。
- ✓ 安全（err/日志不含 headers）：Task 2（`HTTPFetchError` 不含 headers + 单测断言 SECRET 不泄漏）。
- ✓ 列血缘 `output=input∪extract.keys`：Task 4。
- ✓ 绿标（system+user / url+body+headers）：Task 8。
- ✓ 列多下拉框展示全部不省略：Task 8。
- ✓ CLI 同步：Task 6。
- ✓ v2（整节点 merge / 加密凭据 / 更多 method）不在本计划（YAGNI，spec 已切走）。
