# GraphFlow CLI（gf 命令 + SSE 实时联动）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 `gf` 命令行客户端（节点 CRUD/连线、模型配置、数据集、工作流、运行管理全覆盖），CLI 的每次变更通过 SSE 推送让已打开的前端页面实时刷新。

**Architecture:** `gf` 是纯 HTTP 客户端（argparse + httpx），打现有 API，与前端同认证同校验；后端新增内存级每用户订阅队列与 `GET /api/events` SSE 端点，各 mutation 路由提交后 publish 一行；前端用 `EventSource` 订阅，画布页按"指纹比对"判脏决定静默刷新还是提示条。

**Tech Stack:** Python 3.12 / FastAPI / httpx / argparse（无新依赖，httpx 本就是 openai 的传递依赖）；React 18 / vitest。

**Spec:** `docs/superpowers/specs/2026-06-11-graphflow-cli-design.md`

**约束（必须遵守）：** 代码保持 KISS 原则，不预先防御未发生的 bug，无投机抽象。提交信息用中文，用两个 `-m` 参数避免 here-string：`git commit -m "主题" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"`。所有后端命令在 `backend/` 目录下执行，前端命令在 `frontend/` 目录下执行。

---

## 文件结构总览

| 文件 | 职责 |
|---|---|
| 新建 `backend/app/events.py` | 订阅注册表 + publish/subscribe/unsubscribe |
| 新建 `backend/app/routers/events.py` | `GET /api/events` SSE 端点 |
| 修改 `backend/app/main.py` | 挂载 events 路由 |
| 修改 `backend/app/routers/{workflows,model_configs,datasets,runs}.py` | mutation 后 publish |
| 新建 `backend/app/cli.py` | gf CLI 全部命令（单文件） |
| 修改 `backend/pyproject.toml` | build-system、httpx 运行时依赖、`[project.scripts] gf` |
| 修改 `backend/tests/conftest.py` | client fixture 清空订阅表 |
| 新建 `backend/tests/test_events.py` | SSE 测试 |
| 新建 `backend/tests/test_cli.py` | CLI 集成测试（uvicorn 真实端口） |
| 新建 `frontend/src/api/events.ts` | useEvents hook |
| 新建 `frontend/src/canvas/fingerprint.ts` | 图指纹（判脏纯函数） |
| 新建 `frontend/src/canvas/fingerprint.test.ts` | 指纹单测 |
| 修改 `frontend/src/pages/CanvasPage.tsx` | 事件联动 + 提示条 |
| 修改 `frontend/src/pages/{Workflows,Models,Datasets,Runs}Page.tsx` | 收事件重拉列表 |
| 修改 `README.md` | CLI 使用章节 |

---

### Task 1: SSE 基础设施（events.py + /api/events 路由）

**Files:**
- Create: `backend/app/events.py`
- Create: `backend/app/routers/events.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_events.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_events.py`：

```python
import asyncio
import json


async def test_events_requires_auth(client):
    r = await client.get("/api/events")
    assert r.status_code == 401


async def test_stream_receives_published_event(auth_client):
    from app import events

    me = (await auth_client.get("/api/me")).json()
    async with auth_client.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        events.publish(me["id"], "workflow", 1)
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                assert json.loads(line[6:]) == {"entity": "workflow", "id": 1}
                break


async def test_events_isolated_per_user(auth_client):
    from app import events

    a_id = (await auth_client.get("/api/me")).json()["id"]
    async with auth_client.stream("GET", "/api/events"):
        events.publish(a_id + 999, "workflow", 1)  # 发给别的用户
        q = next(iter(events.subscribers[a_id]))
        assert q.qsize() == 0


async def test_disconnect_unsubscribes(auth_client):
    from app import events

    async with auth_client.stream("GET", "/api/events"):
        assert events.subscribers
    for _ in range(100):
        if not events.subscribers:
            break
        await asyncio.sleep(0.01)
    assert not events.subscribers
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_events.py -v
```
预期：4 个测试 FAIL（`ModuleNotFoundError: No module named 'app.events'` 或 404）。

- [ ] **Step 3: 实现 events.py**

创建 `backend/app/events.py`：

```python
import asyncio
import json

subscribers: dict[int, set[asyncio.Queue]] = {}


def publish(user_id: int, entity: str, entity_id: int) -> None:
    for q in subscribers.get(user_id, ()):
        q.put_nowait(json.dumps({"entity": entity, "id": entity_id}))


def subscribe(user_id: int) -> asyncio.Queue:
    q = asyncio.Queue()
    subscribers.setdefault(user_id, set()).add(q)
    return q


def unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    subs = subscribers.get(user_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            del subscribers[user_id]
```

- [ ] **Step 4: 实现 SSE 路由**

创建 `backend/app/routers/events.py`：

```python
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import StreamingResponse

from app import events
from app.auth import COOKIE_NAME, parse_session_cookie

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def event_stream(gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    # 只验签 cookie、不查库：SSE 连接常驻，不能占着数据库会话
    user_id = parse_session_cookie(gf_session) if gf_session else None
    if user_id is None:
        raise HTTPException(status_code=401, detail="未登录")
    q = events.subscribe(user_id)

    async def gen():
        try:
            while True:
                yield f"data: {await q.get()}\n\n"
        finally:
            events.unsubscribe(user_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")
```

- [ ] **Step 5: 挂载路由**

修改 `backend/app/main.py` 第 10 行 import 与 create_app：

```python
from app.routers import auth, datasets, events, model_configs, runs, workflows
```

在 `app.include_router(runs.router)` 之后加：

```python
    app.include_router(events.router)
```

- [ ] **Step 6: conftest 清空订阅表（跨测试隔离全局 dict）**

修改 `backend/tests/conftest.py` 的 `client` fixture，在 `await db.init_db()` 之前加两行：

```python
    from app import events
    events.subscribers.clear()
```

- [ ] **Step 7: 跑测试确认通过**

```
uv run pytest tests/test_events.py -v
```
预期：4 passed。若 `test_disconnect_unsubscribes` 在 ASGITransport 下不收敛（生成器未被取消），如实报告，不要静默放宽断言。

- [ ] **Step 8: 提交**

```
git add backend/app/events.py backend/app/routers/events.py backend/app/main.py backend/tests/conftest.py backend/tests/test_events.py
git commit -m "feat: SSE 事件推送基础设施（/api/events）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: mutation 路由接入 publish

**Files:**
- Modify: `backend/app/routers/workflows.py`
- Modify: `backend/app/routers/model_configs.py`
- Modify: `backend/app/routers/datasets.py`
- Modify: `backend/app/routers/runs.py`
- Test: `backend/tests/test_events.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_events.py` 末尾追加：

```python
async def test_mutations_push_events(auth_client):
    async with auth_client.stream("GET", "/api/events") as resp:
        lines = resp.aiter_lines()

        async def next_event():
            async def read():
                async for line in lines:
                    if line.startswith("data: "):
                        return json.loads(line[6:])
            return await asyncio.wait_for(read(), 5)

        wf = (await auth_client.post("/api/workflows", json={"name": "流"})).json()
        assert await next_event() == {"entity": "workflow", "id": wf["id"]}
        await auth_client.put(f"/api/workflows/{wf['id']}", json={"name": "新名"})
        assert await next_event() == {"entity": "workflow", "id": wf["id"]}
        mc = (await auth_client.post("/api/models", json={
            "name": "m", "model_name": "q", "base_url": "http://x/v1",
            "api_key": "", "default_params": {}})).json()
        assert await next_event() == {"entity": "model", "id": mc["id"]}
        files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
        ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
        assert await next_event() == {"entity": "dataset", "id": ds["id"]}
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_events.py::test_mutations_push_events -v
```
预期：FAIL（5 秒后 `asyncio.TimeoutError`——没收到事件）。

- [ ] **Step 3: 四个路由接 publish**

`backend/app/routers/workflows.py`——import 区加：

```python
from app.events import publish
```

`create_workflow` 的 `await session.commit()` 之后加：

```python
    publish(user.id, "workflow", wf.id)
```

`update_workflow` 的 `await session.commit()` 之后加：

```python
    publish(user.id, "workflow", wf.id)
```

`delete_workflow` 的 `await session.commit()` 之后加：

```python
    publish(user.id, "workflow", wf_id)
```

`backend/app/routers/model_configs.py`——import 区加 `from app.events import publish`；`create_model`、`update_model` 的 commit 后加 `publish(user.id, "model", mc.id)`；`delete_model` 的 commit 后加 `publish(user.id, "model", mc_id)`。

`backend/app/routers/datasets.py`——import 区加 `from app.events import publish`；`upload` 循环内 `results.append(_out(ds))` 之后加 `publish(user.id, "dataset", ds.id)`；`delete_dataset` 的 commit 后加 `publish(user.id, "dataset", ds_id)`。

`backend/app/routers/runs.py`——import 区加 `from app.events import publish`；`create_run` 的 `manager.submit(...)` 之后加 `publish(user.id, "run", run.id)`；`cancel_run` 的 `manager.cancel(run.id)` 之后加 `publish(user.id, "run", run.id)`；`rerun_failed` 的 `manager.submit(...)` 之后加 `publish(user.id, "run", run.id)`。

- [ ] **Step 4: 跑全部后端测试确认通过**

```
uv run pytest -q
```
预期：全部通过（原 84 个 + 新 5 个）。runs 的 publish 行虽无专门断言，但 test_runs_api 全程会执行到它们，写错会炸。

- [ ] **Step 5: 提交**

```
git add backend/app/routers backend/tests/test_events.py
git commit -m "feat: 各 mutation 路由推送变更事件" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: CLI 骨架（pyproject + login/st + 测试基建）

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`

- [ ] **Step 1: pyproject 加 build-system、httpx、console script**

修改 `backend/pyproject.toml`：dependencies 列表末尾加 `"httpx>=0.28"`（dev 组里已有，挪为运行时依赖后 dev 组中可保留不动）；文件末尾追加：

```toml
[project.scripts]
gf = "app.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

- [ ] **Step 2: 验证 uv sync 正常**

```
uv sync
```
预期：成功，graphflow-backend 以可编辑方式装入 venv。

- [ ] **Step 3: 写失败测试（含 uvicorn fixture 与 gf 调用助手）**

创建 `backend/tests/test_cli.py`：

```python
import json
import socket
import sys
import threading
import time

import pytest
import uvicorn

import app.cli as cli
from app.config import settings


def gf(*argv: str):
    cli.main(list(argv))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "cli.json")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from app.main import create_app
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                        log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def test_login_writes_state(server, capsys):
    gf("login", "alice", "--server", server)
    state = json.loads(cli.STATE_FILE.read_text(encoding="utf-8"))
    assert state["server"] == server and state["cookie"]
    assert "已登录 alice" in capsys.readouterr().out


def test_st_shows_user(server, capsys):
    gf("login", "alice", "--server", server)
    capsys.readouterr()
    gf("st")
    assert "alice" in capsys.readouterr().out


def test_st_without_login_dies(server, capsys):
    with pytest.raises(SystemExit) as e:
        gf("st")
    assert e.value.code == 1
    assert "gf login" in capsys.readouterr().err
```

注意：`server` fixture 不依赖 conftest 的 `client`，是独立的真实 uvicorn 进程内线程；monkeypatch 的 `llm.chat` 等跨线程同模块生效。

- [ ] **Step 4: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v
```
预期：FAIL（`No module named 'app.cli'`）。

- [ ] **Step 5: 实现 cli.py 骨架**

创建 `backend/app/cli.py`：

```python
"""gf —— GraphFlow 命令行客户端。所有操作通过 HTTP API 完成，与前端同权限。"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

STATE_FILE = Path.home() / ".graphflow" / "cli.json"
NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "llm_synth": "llm_synth", "auto_process": "auto_process"}
NODE_LABELS = {"input": "输入", "llm_synth": "LLM 合成", "auto_process": "自动处理", "output": "输出"}
KIND_LABELS = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置"}
STATUS_LABELS = {"queued": "排队中", "running": "运行中", "completed": "已完成",
                 "failed": "失败", "cancelled": "已取消", "pending": "等待", "done": "完成"}


def die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


class Cli:
    def __init__(self):
        self.state = load_state()
        if not self.state.get("cookie"):
            die("未登录，先执行: gf login <用户名>")
        self.http = httpx.Client(base_url=self.state["server"],
                                 cookies={"gf_session": self.state["cookie"]}, timeout=30)

    def check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            die(str(detail))
        return r

    def req(self, method: str, path: str, **kw):
        return self.check(self.http.request(method, path, **kw)).json()

    def resolve(self, kind: str, ref: str) -> int:
        """纯数字按 ID，否则按名字精确匹配。kind: workflows/datasets/models。"""
        if ref.isdigit():
            return int(ref)
        hits = [i for i in self.req("GET", f"/api/{kind}") if i["name"] == ref]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            die(f"找不到名为「{ref}」的{KIND_LABELS[kind]}")
        die(f"「{ref}」有 {len(hits)} 个同名项，请改用 ID: {[h['id'] for h in hits]}")

    def current_wf(self) -> int:
        wf_id = self.state.get("workflow_id")
        if not wf_id:
            die("未选择工作流，先执行: gf use <名|ID>")
        return wf_id

    def get_wf(self) -> dict:
        return self.req("GET", f"/api/workflows/{self.current_wf()}")

    def put_graph(self, wf_id: int, graph: dict) -> None:
        self.req("PUT", f"/api/workflows/{wf_id}", json={"graph": graph})


def cmd_login(args):
    server = args.server.rstrip("/")
    r = httpx.post(f"{server}/api/auth/login", json={"username": args.username}, timeout=10)
    if r.status_code >= 400:
        die(f"登录失败: HTTP {r.status_code} {r.text[:200]}")
    state = load_state()
    state.update(server=server, cookie=r.cookies.get("gf_session"))
    save_state(state)
    print(f"已登录 {args.username} @ {server}")


def cmd_st(args):
    cli = Cli()
    me = cli.req("GET", "/api/me")
    line = f"服务器 {cli.state['server']}  用户 {me['username']}"
    wf_id = cli.state.get("workflow_id")
    if wf_id:
        wf = cli.req("GET", f"/api/workflows/{wf_id}")
        line += f"  当前工作流 {wf['name']}（#{wf_id}）"
    print(line)


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(prog="gf", description="GraphFlow 命令行客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="登录")
    s.add_argument("username")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("st", help="当前状态")
    s.set_defaults(func=cmd_st)

    args = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # 启用 conhost 的 ANSI 转义支持（watch 进度刷新用）
    try:
        args.func(args)
    except httpx.ConnectError:
        die("无法连接服务器，请确认 GraphFlow 已启动")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：3 passed。

- [ ] **Step 7: 手动验证 console script**

```
uv run gf --help
```
预期：打印用法，含 login/st 子命令。

- [ ] **Step 8: 提交**

```
git add backend/pyproject.toml backend/uv.lock backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf CLI 骨架（login/st、状态文件、console script）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: wf / use / show 命令

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_cli.py` 末尾追加：

```python
def login_and_wf(server: str, name: str = "流"):
    gf("login", "tester", "--server", server)
    gf("wf", "add", name)
    gf("use", name)


def test_wf_lifecycle(server, capsys):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流A")
    gf("use", "流A")
    capsys.readouterr()
    gf("st")
    assert "流A" in capsys.readouterr().out
    gf("wf", "ls")
    assert "流A" in capsys.readouterr().out
    gf("wf", "rm", "流A")
    capsys.readouterr()
    gf("wf", "ls")
    assert "流A" not in capsys.readouterr().out


def test_use_unknown_name_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit) as e:
        gf("use", "不存在的流")
    assert e.value.code == 1
    assert "找不到名为" in capsys.readouterr().err


def test_show_lists_nodes_and_edges(server, capsys):
    login_and_wf(server)
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "节点（0）" in out and "连线（0）" in out
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k "wf or use or show"
```
预期：FAIL（argparse: invalid choice 'wf'，SystemExit 码为 2 而非预期行为）。

- [ ] **Step 3: 实现命令**

在 `backend/app/cli.py` 的 `cmd_st` 之后加处理函数：

```python
def cmd_wf_ls(args):
    cli = Cli()
    for w in cli.req("GET", "/api/workflows"):
        print(f"{w['id']:>4}  {w['name']}  {w['updated_at'][:19]}")


def cmd_wf_add(args):
    cli = Cli()
    w = cli.req("POST", "/api/workflows", json={"name": args.name})
    print(f"已创建工作流 {w['name']}（#{w['id']}）")


def cmd_wf_rm(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    cli.req("DELETE", f"/api/workflows/{wf_id}")
    print(f"已删除工作流 #{wf_id}")


def cmd_use(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    wf = cli.req("GET", f"/api/workflows/{wf_id}")
    cli.state["workflow_id"] = wf_id
    save_state(cli.state)
    print(f"当前工作流: {wf['name']}（#{wf_id}）")


def summarize(n: dict) -> str:
    c = n["config"]
    if n["type"] == "input":
        return f"数据集 {c.get('dataset_ids', [])}"
    if n["type"] == "llm_synth":
        return f"模型 #{c.get('model_config_id', '?')} -> {c.get('output_column', 'output')}"
    if n["type"] == "auto_process":
        return f"{len(c.get('operations', []))} 个操作"
    return f"保存为数据集「{c['dataset_name']}」" if c.get("save_as_dataset") else ""


def cmd_show(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    print(f"工作流 {wf['name']}（#{wf['id']}）")
    print(f"节点（{len(graph['nodes'])}）:")
    for n in graph["nodes"]:
        print(f"  {n['id']}  [{NODE_LABELS[n['type']]}]  {summarize(n)}")
    print(f"连线（{len(graph['edges'])}）:")
    for e in graph["edges"]:
        print(f"  {e['source']} -> {e['target']}")
```

在 `main()` 中 `st` 子命令之后追加注册：

```python
    wf = sub.add_parser("wf", help="工作流管理").add_subparsers(dest="action", required=True)
    s = wf.add_parser("ls"); s.set_defaults(func=cmd_wf_ls)
    s = wf.add_parser("add"); s.add_argument("name"); s.set_defaults(func=cmd_wf_add)
    s = wf.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_wf_rm)

    s = sub.add_parser("use", help="设当前工作流")
    s.add_argument("ref")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("show", help="查看当前工作流图")
    s.set_defaults(func=cmd_show)
```

- [ ] **Step 4: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：7 passed。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf wf/use/show 命令" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: node / link / unlink 命令

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_cli.py` 末尾追加：

```python
def test_node_add_set_show(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    capsys.readouterr()
    gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "conc=8", "temp=0.5", "out=a")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["user_prompt"] == "Q:{{q}}"
    assert node["config"]["concurrency"] == 8
    assert node["config"]["params"]["temperature"] == 0.5
    assert node["config"]["output_column"] == "a"


def test_node_auto_numbering(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "add", "llm")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "llm_synth_1" in out and "llm_synth_2" in out


def test_node_set_unknown_key_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit):
        gf("node", "set", "llm_synth_1", "nosuch=1")
    assert "未知配置键" in capsys.readouterr().err


def test_link_unlink_and_rm_cleans_edges(server, capsys):
    login_and_wf(server)
    gf("node", "add", "input")
    gf("node", "add", "llm")
    gf("link", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    assert "input_1 -> llm_synth_1" in capsys.readouterr().out
    gf("unlink", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    assert "input_1 -> llm_synth_1" not in capsys.readouterr().out
    gf("link", "input_1", "llm_synth_1")
    gf("node", "rm", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "llm_synth_1" not in out and "->" not in out


def test_node_add_without_use_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit):
        gf("node", "add", "llm")
    assert "gf use" in capsys.readouterr().err
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k "node or link"
```
预期：FAIL（invalid choice: 'node'）。

- [ ] **Step 3: 实现命令**

在 `backend/app/cli.py` 加（`cmd_show` 之后）：

```python
LLM_CONFIG_KEYS = {"system": "system_prompt", "prompt": "user_prompt", "out": "output_column",
                   "mode": "output_mode", "fanout": "fanout_n", "conc": "concurrency",
                   "retries": "retries"}
LLM_PARAM_KEYS = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
                  "timeout": "timeout", "json_mode": "json_mode"}
INT_KEYS = {"fanout_n", "concurrency", "retries", "max_tokens", "timeout"}
FLOAT_KEYS = {"temperature", "top_p"}


def convert(field: str, v: str):
    if field in INT_KEYS:
        return int(v)
    if field in FLOAT_KEYS:
        return float(v)
    if field == "json_mode":
        return v.lower() in ("true", "1", "yes")
    return v


def parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"参数格式应为 key=value: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    die(f"节点 {node_id} 不存在")


def cmd_node_add(args):
    cli = Cli()
    ntype = NODE_TYPES.get(args.type)
    if ntype is None:
        die(f"未知节点类型 {args.type}（可选: input/llm/auto/output）")
    wf = cli.get_wf()
    nodes = wf["graph"]["nodes"]
    if args.id:
        node_id = args.id
        if any(n["id"] == node_id for n in nodes):
            die(f"节点 {node_id} 已存在")
    else:
        i = 1
        while any(n["id"] == f"{ntype}_{i}" for n in nodes):
            i += 1
        node_id = f"{ntype}_{i}"
    nodes.append({"id": node_id, "type": ntype,
                  "position": {"x": 80 + len(nodes) * 50, "y": 80 + len(nodes) * 40},
                  "config": {}})
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加节点 {node_id}")


def cmd_node_set(args):
    cli = Cli()
    wf = cli.get_wf()
    node = find_node(wf["graph"], args.id)
    cfg = node["config"]
    for k, v in parse_kv(args.pairs).items():
        if k == "dataset":
            cfg["dataset_ids"] = [cli.resolve("datasets", r) for r in v.split(",") if r]
        elif k == "model":
            cfg["model_config_id"] = cli.resolve("models", v)
        elif k == "save_as":
            cfg["save_as_dataset"] = bool(v)
            cfg["dataset_name"] = v
        elif k in LLM_CONFIG_KEYS:
            cfg[LLM_CONFIG_KEYS[k]] = convert(LLM_CONFIG_KEYS[k], v)
        elif k in LLM_PARAM_KEYS:
            cfg.setdefault("params", {})[LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        else:
            die(f"未知配置键 {k}")
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已更新 {args.id}: {json.dumps(cfg, ensure_ascii=False)}")


def cmd_node_show(args):
    cli = Cli()
    node = find_node(cli.get_wf()["graph"], args.id)
    print(json.dumps(node, ensure_ascii=False, indent=2))


def cmd_node_rm(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    find_node(graph, args.id)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != args.id]
    graph["edges"] = [e for e in graph["edges"] if args.id not in (e["source"], e["target"])]
    cli.put_graph(wf["id"], graph)
    print(f"已删除节点 {args.id} 及其连线")


def cmd_link(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    find_node(graph, args.source)
    find_node(graph, args.target)
    if any(e["source"] == args.source and e["target"] == args.target for e in graph["edges"]):
        die("连线已存在")
    graph["edges"].append({"source": args.source, "target": args.target, "kind": "normal"})
    cli.put_graph(wf["id"], graph)
    print(f"已连线 {args.source} -> {args.target}")


def cmd_unlink(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    before = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"]
                      if not (e["source"] == args.source and e["target"] == args.target)]
    if len(graph["edges"]) == before:
        die(f"不存在连线 {args.source} -> {args.target}")
    cli.put_graph(wf["id"], graph)
    print(f"已断开 {args.source} -> {args.target}")
```

`main()` 中追加注册（`show` 之后）：

```python
    node = sub.add_parser("node", help="节点管理").add_subparsers(dest="action", required=True)
    s = node.add_parser("add"); s.add_argument("type"); s.add_argument("id", nargs="?"); s.set_defaults(func=cmd_node_add)
    s = node.add_parser("set"); s.add_argument("id"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_node_set)
    s = node.add_parser("show"); s.add_argument("id"); s.set_defaults(func=cmd_node_show)
    s = node.add_parser("rm"); s.add_argument("id"); s.set_defaults(func=cmd_node_rm)

    s = sub.add_parser("link", help="连线")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="断开连线")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(func=cmd_unlink)
```

- [ ] **Step 4: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：12 passed。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf node/link/unlink 命令与键名映射" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: op 命令（自动处理操作）

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_op_lifecycle(server, capsys):
    login_and_wf(server)
    gf("node", "add", "auto")
    gf("op", "add", "auto_process_1", "dedup", "q")
    gf("op", "add", "auto_process_1", "filter", "q", "min_len", "5")
    gf("op", "add", "auto_process_1", "shuffle")
    capsys.readouterr()
    gf("op", "ls", "auto_process_1")
    out = capsys.readouterr().out
    assert "1. 去重" in out and "2. 过滤" in out and "3. 打乱" in out
    gf("node", "show", "auto_process_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["operations"][1] == {"op": "filter", "column": "q",
                                               "mode": "min_len", "value": 5}
    gf("op", "rm", "auto_process_1", "1")
    capsys.readouterr()
    gf("op", "ls", "auto_process_1")
    out = capsys.readouterr().out
    assert "去重" not in out and "1. 过滤" in out


def test_op_on_non_auto_node_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit):
        gf("op", "add", "llm_synth_1", "shuffle")
    assert "不是自动处理节点" in capsys.readouterr().err
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k op
```
预期：FAIL（invalid choice: 'op'）。

- [ ] **Step 3: 实现命令**

在 `backend/app/cli.py` 加：

```python
OP_LABELS = {"dedup": "去重", "filter": "过滤", "rename": "重命名", "drop": "删除列",
             "concat": "拼接列", "cast": "类型转换", "sample": "随机采样", "shuffle": "打乱"}


def build_op(op: str, params: list[str]) -> dict:
    if op == "dedup":
        return {"op": "dedup", "columns": params[0].split(",") if params else []}
    if op == "filter":
        if len(params) != 3:
            die("用法: gf op add <节点> filter <列> <min_len|max_len|contains|not_contains|regex> <值>")
        col, mode, value = params
        return {"op": "filter", "column": col, "mode": mode,
                "value": int(value) if mode in ("min_len", "max_len") else value}
    if op == "rename":
        if len(params) != 2:
            die("用法: gf op add <节点> rename <原列> <新列>")
        return {"op": "rename", "mapping": {params[0]: params[1]}}
    if op == "drop":
        if len(params) != 1:
            die("用法: gf op add <节点> drop <列1,列2>")
        return {"op": "drop", "columns": params[0].split(",")}
    if op == "concat":
        if len(params) < 2:
            die("用法: gf op add <节点> concat <列1,列2> <目标列> [分隔符]")
        return {"op": "concat", "columns": params[0].split(","), "target": params[1],
                "sep": params[2] if len(params) > 2 else ""}
    if op == "cast":
        if len(params) != 2 or params[1] not in ("str", "int", "float"):
            die("用法: gf op add <节点> cast <列> <str|int|float>")
        return {"op": "cast", "column": params[0], "to": params[1]}
    if op == "sample":
        if len(params) != 1:
            die("用法: gf op add <节点> sample <n>")
        return {"op": "sample", "n": int(params[0])}
    if op == "shuffle":
        return {"op": "shuffle"}
    die(f"未知操作 {op}（可选: dedup/filter/rename/drop/concat/cast/sample/shuffle）")


def _auto_node(cli: Cli, node_id: str) -> tuple[dict, list]:
    wf = cli.get_wf()
    node = find_node(wf["graph"], node_id)
    if node["type"] != "auto_process":
        die(f"{node_id} 不是自动处理节点")
    return wf, node["config"].setdefault("operations", [])


def cmd_op_add(args):
    cli = Cli()
    wf, ops = _auto_node(cli, args.node_id)
    ops.append(build_op(args.op, args.params))
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加操作 #{len(ops)}: {json.dumps(ops[-1], ensure_ascii=False)}")


def cmd_op_ls(args):
    cli = Cli()
    _, ops = _auto_node(cli, args.node_id)
    for i, o in enumerate(ops, 1):
        rest = {k: v for k, v in o.items() if k != "op"}
        print(f"{i}. {OP_LABELS[o['op']]} {json.dumps(rest, ensure_ascii=False)}")


def cmd_op_rm(args):
    cli = Cli()
    wf, ops = _auto_node(cli, args.node_id)
    if not 1 <= args.index <= len(ops):
        die(f"序号超出范围（1-{len(ops)}）")
    removed = ops.pop(args.index - 1)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已删除操作: {OP_LABELS[removed['op']]}")
```

`main()` 追加注册：

```python
    op = sub.add_parser("op", help="自动处理操作").add_subparsers(dest="action", required=True)
    s = op.add_parser("add"); s.add_argument("node_id"); s.add_argument("op"); s.add_argument("params", nargs="*"); s.set_defaults(func=cmd_op_add)
    s = op.add_parser("ls"); s.add_argument("node_id"); s.set_defaults(func=cmd_op_ls)
    s = op.add_parser("rm"); s.add_argument("node_id"); s.add_argument("index", type=int); s.set_defaults(func=cmd_op_rm)
```

- [ ] **Step 4: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：14 passed。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf op 命令（8 种自动处理操作）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: model 命令

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_model_lifecycle(server, capsys):
    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    capsys.readouterr()
    gf("model", "ls")
    out = capsys.readouterr().out
    assert "通义" in out and "qwen" in out and "key:已配置" in out
    gf("model", "set", "通义", "model=qwen-max", "temp=0.7")
    capsys.readouterr()
    gf("model", "ls")
    out = capsys.readouterr().out
    assert "qwen-max" in out and "key:已配置" in out  # api_key 留空不覆盖
    gf("model", "rm", "通义")
    capsys.readouterr()
    gf("model", "ls")
    assert "通义" not in capsys.readouterr().out


def test_model_test_reports_result(server, capsys, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return "pong", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake_chat)
    gf("login", "tester", "--server", server)
    gf("model", "add", "m", "--url", "http://x/v1", "--model", "q", "--key", "k")
    capsys.readouterr()
    gf("model", "test", "m")
    assert "连通正常" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k model
```
预期：FAIL（invalid choice: 'model'）。

- [ ] **Step 3: 实现命令**

```python
MODEL_KEYS = {"name": "name", "model": "model_name", "url": "base_url", "key": "api_key"}


def cmd_model_ls(args):
    cli = Cli()
    for m in cli.req("GET", "/api/models"):
        key = "已配置" if m["api_key_set"] else "未配置"
        print(f"{m['id']:>4}  {m['name']}  {m['model_name']}  {m['base_url']}  key:{key}")


def cmd_model_add(args):
    cli = Cli()
    m = cli.req("POST", "/api/models", json={
        "name": args.name, "model_name": args.model, "base_url": args.url,
        "api_key": args.key, "default_params": {}})
    print(f"已创建模型配置 {m['name']}（#{m['id']}）")


def cmd_model_set(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    hits = [m for m in cli.req("GET", "/api/models") if m["id"] == mc_id]
    if not hits:
        die("模型配置不存在")
    cur = hits[0]
    body = {"name": cur["name"], "model_name": cur["model_name"], "base_url": cur["base_url"],
            "api_key": "", "default_params": cur["default_params"]}  # key 留空=不修改
    for k, v in parse_kv(args.pairs).items():
        if k in MODEL_KEYS:
            body[MODEL_KEYS[k]] = v
        elif k in LLM_PARAM_KEYS:
            body["default_params"][LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        else:
            die(f"未知配置键 {k}")
    cli.req("PUT", f"/api/models/{mc_id}", json=body)
    print("已更新")


def cmd_model_rm(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    cli.req("DELETE", f"/api/models/{mc_id}")
    print(f"已删除模型配置 #{mc_id}")


def cmd_model_test(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    r = cli.req("POST", f"/api/models/{mc_id}/test")
    if r["ok"]:
        print(f"连通正常: {r['reply']}")
    else:
        die(f"连接失败: {r['error']}")
```

`main()` 追加注册：

```python
    model = sub.add_parser("model", help="模型配置").add_subparsers(dest="action", required=True)
    s = model.add_parser("ls"); s.set_defaults(func=cmd_model_ls)
    s = model.add_parser("add"); s.add_argument("name"); s.add_argument("--url", required=True); s.add_argument("--model", required=True); s.add_argument("--key", default=""); s.set_defaults(func=cmd_model_add)
    s = model.add_parser("set"); s.add_argument("ref"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_model_set)
    s = model.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_model_rm)
    s = model.add_parser("test"); s.add_argument("ref"); s.set_defaults(func=cmd_model_test)
```

- [ ] **Step 4: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：16 passed。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf model 命令（增删改查与连通测试）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: data 命令

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_data_up_head_rm(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    f = tmp_path / "种子.jsonl"
    f.write_text('{"q": "问0"}\n{"q": "问1"}\n{"q": "问2"}\n', encoding="utf-8")
    gf("data", "up", str(f))
    assert "已上传 种子" in capsys.readouterr().out
    gf("data", "ls")
    out = capsys.readouterr().out
    assert "种子" in out and "3 行" in out
    gf("data", "head", "种子", "2")
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["q"] == "问0"
    gf("data", "rm", "种子")
    capsys.readouterr()
    gf("data", "ls")
    assert "种子" not in capsys.readouterr().out


def test_data_up_missing_file_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit):
        gf("data", "up", "不存在.jsonl")
    assert "文件不存在" in capsys.readouterr().err
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k data
```
预期：FAIL（invalid choice: 'data'）。

- [ ] **Step 3: 实现命令**

```python
def cmd_data_ls(args):
    cli = Cli()
    for d in cli.req("GET", "/api/datasets"):
        print(f"{d['id']:>4}  {d['name']}  {d['row_count']} 行  [{','.join(d['columns'])}]")


def cmd_data_up(args):
    cli = Cli()
    files = []
    for p in args.files:
        path = Path(p)
        if not path.is_file():
            die(f"文件不存在: {p}")
        files.append(("files", (path.name, path.read_bytes())))
    for d in cli.req("POST", "/api/datasets/upload", files=files):
        print(f"已上传 {d['name']}（#{d['id']}，{d['row_count']} 行）")


def cmd_data_head(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    page = cli.req("GET", f"/api/datasets/{ds_id}/rows", params={"page": 1, "page_size": args.n})
    for row in page["rows"]:
        print(json.dumps(row, ensure_ascii=False))


def cmd_data_rm(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    cli.req("DELETE", f"/api/datasets/{ds_id}")
    print(f"已删除数据集 #{ds_id}")
```

`main()` 追加注册：

```python
    data = sub.add_parser("data", help="数据集").add_subparsers(dest="action", required=True)
    s = data.add_parser("ls"); s.set_defaults(func=cmd_data_ls)
    s = data.add_parser("up"); s.add_argument("files", nargs="+"); s.set_defaults(func=cmd_data_up)
    s = data.add_parser("head"); s.add_argument("ref"); s.add_argument("n", nargs="?", type=int, default=5); s.set_defaults(func=cmd_data_head)
    s = data.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_data_rm)
```

- [ ] **Step 4: 跑测试确认通过**

```
uv run pytest tests/test_cli.py -v
```
预期：18 passed。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf data 命令（上传/预览/删除）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: run / runs / watch / cancel / rerun / export + 全链路验收

**Files:**
- Modify: `backend/app/cli.py`
- Test: `backend/tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试（全链路验收 = 设计文档 §11 标准 1）**

```python
def test_cli_full_chain(server, capsys, tmp_path, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n{"q": "问1"}\n', encoding="utf-8")

    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    gf("data", "up", str(seed))
    gf("wf", "add", "翻译流水线")
    gf("use", "翻译流水线")
    gf("node", "add", "input")
    gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "model=通义", "out=a")
    gf("node", "add", "output")
    gf("link", "input_1", "llm_synth_1")
    gf("link", "llm_synth_1", "output_1")
    capsys.readouterr()
    gf("run", "-f")
    out = capsys.readouterr().out
    assert "已启动" in out and "已完成" in out

    export_path = tmp_path / "导出.jsonl"
    gf("export", "1", "-o", str(export_path))
    lines = [json.loads(l) for l in
             export_path.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2 and lines[0]["a"] == "答[Q:问0]"

    capsys.readouterr()
    gf("runs")
    assert "翻译流水线" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        gf("cancel", "1")
    assert "不可取消" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        gf("rerun", "1")
    assert "没有失败行" in capsys.readouterr().err


def test_watch_without_runs_dies(server, capsys):
    login_and_wf(server)
    with pytest.raises(SystemExit):
        gf("watch")
    assert "还没有运行记录" in capsys.readouterr().err
```

- [ ] **Step 2: 跑测试确认失败**

```
uv run pytest tests/test_cli.py -v -k "full_chain or watch_without"
```
预期：FAIL（invalid choice: 'run'）。

- [ ] **Step 3: 实现命令**

```python
def watch_run(cli: Cli, run_id: int):
    lines = 0
    while True:
        d = cli.req("GET", f"/api/runs/{run_id}")
        if lines:
            print(f"\x1b[{lines}F\x1b[J", end="")  # 光标回退并清除旧进度表
        rows = [f"  {s['node_id']:<18} {STATUS_LABELS.get(s['status'], s['status']):<4} "
                f"{s['done']}/{s['total']}" + (f" 失败{s['failed']}" if s["failed"] else "")
                for s in d["node_states"]]
        print("\n".join([f"运行 #{run_id}  {STATUS_LABELS.get(d['status'], d['status'])}"] + rows))
        lines = 1 + len(rows)
        if d["status"] in ("completed", "failed", "cancelled"):
            if d["error"]:
                print(f"错误: {d['error']}")
            return
        time.sleep(1)


def cmd_run(args):
    cli = Cli()
    r = cli.req("POST", "/api/runs", json={"workflow_id": cli.current_wf()})
    print(f"运行 #{r['id']} 已启动")
    if args.follow:
        watch_run(cli, r["id"])


def cmd_runs(args):
    cli = Cli()
    for r in cli.req("GET", "/api/runs"):
        print(f"{r['id']:>4}  {r['workflow_name']}  "
              f"{STATUS_LABELS.get(r['status'], r['status'])}  {r['created_at'][:19]}")


def cmd_watch(args):
    cli = Cli()
    run_id = args.run_id
    if run_id is None:
        runs = cli.req("GET", "/api/runs", params={"workflow_id": cli.current_wf()})
        if not runs:
            die("当前工作流还没有运行记录")
        run_id = runs[0]["id"]
    watch_run(cli, run_id)


def cmd_cancel(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/cancel")
    print(f"已请求取消运行 #{args.run_id}")


def cmd_rerun(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/rerun-failed")
    print(f"运行 #{args.run_id} 失败行已重新排队")


def cmd_export(args):
    cli = Cli()
    params = {"format": args.format}
    if args.node:
        params["node_id"] = args.node
    r = cli.check(cli.http.get(f"/api/runs/{args.run_id}/export", params=params))
    out = Path(args.output or f"run{args.run_id}.{args.format}")
    out.write_bytes(r.content)
    print(f"已导出 {out}（{len(r.content)} 字节）")
```

`main()` 追加注册：

```python
    s = sub.add_parser("run", help="运行当前工作流")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("runs", help="运行列表")
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("watch", help="跟随运行进度")
    s.add_argument("run_id", nargs="?", type=int)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("cancel", help="取消运行")
    s.add_argument("run_id", type=int)
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("rerun", help="重跑失败行")
    s.add_argument("run_id", type=int)
    s.set_defaults(func=cmd_rerun)

    s = sub.add_parser("export", help="导出运行结果")
    s.add_argument("run_id", type=int)
    s.add_argument("-o", "--output")
    s.add_argument("--format", default="jsonl", choices=["jsonl", "csv", "xlsx"])
    s.add_argument("--node")
    s.set_defaults(func=cmd_export)
```

- [ ] **Step 4: 跑全部后端测试确认通过**

```
uv run pytest -q
```
预期：全部通过（CLI 测试共 20 个）。

- [ ] **Step 5: 提交**

```
git add backend/app/cli.py backend/tests/test_cli.py
git commit -m "feat: gf run/watch/cancel/rerun/export 命令与全链路验收" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: 前端 fingerprint + useEvents

**Files:**
- Create: `frontend/src/canvas/fingerprint.ts`
- Create: `frontend/src/canvas/fingerprint.test.ts`
- Create: `frontend/src/api/events.ts`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/canvas/fingerprint.test.ts`：

```ts
import { graphFingerprint } from './fingerprint'
import type { WorkflowGraph } from '../api/types'

const graph: WorkflowGraph = {
  nodes: [{ id: 'in', type: 'input', position: { x: 1, y: 2 }, config: { dataset_ids: [1] } }],
  edges: [{ source: 'in', target: 'out', kind: 'normal' }],
}

test('同一图指纹一致（与对象键序无关）', () => {
  const reordered = {
    edges: [{ kind: 'normal', target: 'out', source: 'in' }],
    nodes: [{ config: { dataset_ids: [1] }, position: { x: 1, y: 2 }, type: 'input', id: 'in' }],
  } as WorkflowGraph
  expect(graphFingerprint(reordered)).toBe(graphFingerprint(graph))
})

test('位置变化改变指纹', () => {
  const moved = JSON.parse(JSON.stringify(graph)) as WorkflowGraph
  moved.nodes[0].position.x = 99
  expect(graphFingerprint(moved)).not.toBe(graphFingerprint(graph))
})
```

- [ ] **Step 2: 跑测试确认失败**

```
npx vitest run src/canvas/fingerprint.test.ts
```
预期：FAIL（Cannot find module './fingerprint'）。

- [ ] **Step 3: 实现 fingerprint.ts**

创建 `frontend/src/canvas/fingerprint.ts`：

```ts
import type { WorkflowGraph } from '../api/types'
import { fromFlow, toFlow } from './serialize'

// 经同一序列化路径归一后 stringify：键序、字段集合一致，可用于画布判脏
export function graphFingerprint(graph: WorkflowGraph): string {
  const f = toFlow(graph)
  return JSON.stringify(fromFlow(f.nodes, f.edges))
}
```

- [ ] **Step 4: 跑测试确认通过**

```
npx vitest run src/canvas/fingerprint.test.ts
```
预期：2 passed。

- [ ] **Step 5: 实现 useEvents（hook 薄封装，不单测，由页面联动验证）**

创建 `frontend/src/api/events.ts`：

```ts
import { useEffect, useRef } from 'react'

export interface GfEvent {
  entity: 'workflow' | 'model' | 'dataset' | 'run'
  id: number
}

export function useEvents(handler: (e: GfEvent) => void) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const es = new EventSource('/api/events')
    es.onmessage = (m) => ref.current(JSON.parse(m.data) as GfEvent)
    return () => es.close()
  }, [])
}
```

- [ ] **Step 6: 类型检查**

```
npx tsc --noEmit -p tsconfig.app.json
```
预期：无输出（通过）。

- [ ] **Step 7: 提交**

```
git add frontend/src/canvas/fingerprint.ts frontend/src/canvas/fingerprint.test.ts frontend/src/api/events.ts
git commit -m "feat: 前端图指纹与 useEvents 订阅 hook" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: 前端页面接入事件

**Files:**
- Modify: `frontend/src/pages/CanvasPage.tsx`
- Modify: `frontend/src/pages/WorkflowsPage.tsx`
- Modify: `frontend/src/pages/ModelsPage.tsx`
- Modify: `frontend/src/pages/DatasetsPage.tsx`
- Modify: `frontend/src/pages/RunsPage.tsx`

- [ ] **Step 1: CanvasPage 接入（核心改动）**

修改 `frontend/src/pages/CanvasPage.tsx`，Canvas 组件整体替换为（仅展示有变化的部分，其余 onConnect/addNode/run/updateConfig/JSX-Drawer 不动）：

import 区改为：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Space, message } from 'antd'
```

并新增：

```tsx
import { useEvents } from '../api/events'
import { graphFingerprint } from '../canvas/fingerprint'
```

Canvas 组件内，原 `useEffect` 加载逻辑替换为：

```tsx
  const [cliChanged, setCliChanged] = useState(false)
  const baseline = useRef('')

  const load = useCallback(async () => {
    const w = await api.get<Workflow>(`/api/workflows/${id}`)
    setWf(w)
    const f = toFlow(w.graph)
    setNodes(f.nodes)
    setEdges(f.edges)
    baseline.current = graphFingerprint(w.graph)
    setCliChanged(false)
  }, [id, setNodes, setEdges])

  useEffect(() => {
    void load()
  }, [load])

  useEvents((e) => {
    if (e.entity !== 'workflow' || e.id !== Number(id)) return
    if (graphFingerprint(fromFlow(nodes, edges)) === baseline.current) void load()
    else setCliChanged(true)
  })
```

`save` 替换为（先记基线再 PUT：自身保存触发的回声事件视为干净）：

```tsx
  const save = async () => {
    const graph = fromFlow(nodes, edges)
    baseline.current = graphFingerprint(graph)
    await api.put(`/api/workflows/${id}`, { graph })
    message.success('已保存')
  }
```

JSX 最外层 `<div>` 内、`<Space>` 之前加提示条：

```tsx
      {cliChanged && (
        <Alert
          type="info" showIcon style={{ marginBottom: 8 }}
          message="工作流已被 CLI 修改"
          action={<Button size="small" type="primary" onClick={() => void load()}>加载最新版本</Button>}
        />
      )}
```

- [ ] **Step 2: 四个列表页接入（各一行 hook）**

`WorkflowsPage.tsx`：import 加 `import { useEvents } from '../api/events'`；`useEffect` 之后加：

```tsx
  useEvents((e) => {
    if (e.entity === 'workflow') void reload()
  })
```

`ModelsPage.tsx`：同上，条件为 `e.entity === 'model'`。

`DatasetsPage.tsx`：同上，条件为 `e.entity === 'dataset'`。

`RunsPage.tsx`：加载逻辑提取为 reload 再接 hook——import 区加 `useCallback` 与 useEvents，组件内改为：

```tsx
  const reload = useCallback(
    () => api.get<Run[]>(`/api/runs${wfId ? `?workflow_id=${wfId}` : ''}`).then(setList),
    [wfId],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  useEvents((e) => {
    if (e.entity === 'run') void reload()
  })
```

- [ ] **Step 3: 类型检查 + 测试 + 构建**

```
npx tsc --noEmit -p tsconfig.app.json
npx vitest run
npm run build
```
预期：全部通过，构建产物输出到 `backend/static`。

- [ ] **Step 4: 手动冒烟（验收标准 2）**

后端目录 `uv run uvicorn app.main:app --port 8000`，浏览器开 `http://127.0.0.1:8000` 登录并打开某工作流画布；另开终端 `uv run gf login 同名用户; uv run gf use 该工作流; uv run gf node add llm`——画布应在 1 秒内出现新节点；在画布上拖动节点（不保存）后再执行一次 CLI 修改——应出现提示条而非覆盖。验证后关闭服务。

- [ ] **Step 5: 提交**

```
git add frontend/src/pages
git commit -m "feat: 前端五页接入 SSE 事件实时联动" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: README 与全量回归

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README 加 CLI 章节**

在开发启动章节之后插入：

````markdown
## 命令行工具 gf

在 `backend/` 目录内用 `uv run gf …`，或安装为全局命令：`cd backend; uv tool install -e .`。

```powershell
uv run gf login alice                 # 登录（默认 http://127.0.0.1:8000，--server 可改）
uv run gf wf add 翻译流水线
uv run gf use 翻译流水线              # 设当前工作流，后续命令默认作用于它
uv run gf node add input
uv run gf node set input_1 dataset=种子集
uv run gf node add llm
uv run gf node set llm_synth_1 model=通义 "prompt=把{{q}}翻译成英文" out=answer
uv run gf node add output
uv run gf link input_1 llm_synth_1
uv run gf link llm_synth_1 output_1
uv run gf run -f                      # 运行并跟随进度
uv run gf export 1 --format jsonl
```

`gf --help` 与 `gf <子命令> --help` 查看全部命令。浏览器中已打开的页面会通过
SSE 推送实时反映 CLI 的修改；画布上有未保存改动时不会被覆盖，而是显示提示条。
````

- [ ] **Step 2: 全量回归**

```
uv run pytest -q          # backend/ 目录
npx vitest run            # frontend/ 目录
npx tsc --noEmit -p tsconfig.app.json
npm run build
```
预期：后端约 109 个测试全过；前端 6 个测试全过；构建成功。

- [ ] **Step 3: 提交**

```
git add README.md
git commit -m "docs: README 增补 gf CLI 使用章节" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 验收清单（对照设计文档 §11）

1. 仅用 gf 完成 登录→模型→数据集→工作流→节点连线→运行→跟随→导出：`test_cli_full_chain` 覆盖。
2. 画布实时刷新 / 提示条：Task 11 Step 4 手动冒烟覆盖。
3. CLI 与前端同权限（跨用户 404/隔离）：CLI 走同一套 API 端点（既有测试覆盖）+ `test_events_isolated_per_user`。
4. api_key 只写不读：CLI 输出仅含 `api_key_set` 派生的「已配置/未配置」（`cmd_model_ls`），无明文 key 字段可打印。
