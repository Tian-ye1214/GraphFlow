# 可复用提示词库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户可创建、编辑、版本化、复用提示词；工作流节点的 system/user 提示词可从库**复制**或**引用**（运行时取最新版），配套管理页、API、`gf prompt` CLI 与 `gf-prompt` 技能。

**Architecture:** 后端 `models.py` 加 `Prompt`/`PromptVersion` 两表（正文在版本里，当前=最新版），新 `routers/prompts.py` 提供 CRUD+版本+回滚+复制+被引用扫描。节点 config 新增 `system_prompt_ref`/`user_prompt_ref` 键；`engine/runner.py` 在 topo 循环前解析引用→填最新版正文，缺失则整 run 报错。前端新 `PromptsPage`（左列表+右分栏+md 预览+版本面板）+ 节点表单「从库」复制/引用控件。CLI 新 `gf prompt` 全套 + `gf node prompt --library`。

**Tech Stack:** FastAPI + SQLAlchemy2 async（SQLite，`Base.metadata.create_all` 自动建表，无 migration）；React + antd + react-markdown（已装）；argparse CLI（httpx，`trust_env=False`）；pytest（后端，真实 in-process server）+ vitest（前端纯函数单测）。

## Global Constraints

- KISS；无防御性代码（不为不会发生的 bug 写防护）。
- 所有资源 **user_id 租户隔离**（硬红线）：新端点 `_get_owned` 在访问行数据前先 404。
- API Key / Authorization 绝不进任何日志/响应/输出/提示词。
- 测试只本地，**不推 origin**；不重新 pull origin（会删本地测试）。
- `backend/tests/` 被 gitignore，**新后端测试文件需 `git add -f`**；`frontend/src/` 正常 tracked，前端测试正常 `git add`。
- 提交记录**不出现 claude**，**不加 Co-Authored-By 尾注**。
- 绝不 `git add` 项目设计.txt / `.idea/` / `.codegraph/` / `.git/sdd/` / `backend/uv.lock`（本批不动）。
- 资源解析（CLI）：纯数字按 ID，否则按名精确匹配，重名报候选。
- 提示词正文里只用 `{{列名}}` 占位符，渲染逻辑沿用引擎不变。

**后端测试命令：** `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider <路径>`
**前端测试命令：** `cd "E:/代码/GraphFlow/frontend" && npx vitest run <路径>`

---

### Task 1: 数据模型 + 提示词 CRUD API

**Files:**
- Modify: `backend/app/models.py`（文件末尾追加两表）
- Create: `backend/app/routers/prompts.py`
- Modify: `backend/app/main.py:11-12,29`（注册 router）
- Test: `backend/tests/test_prompts_api.py`

**Interfaces:**
- Produces:
  - `Prompt(id, user_id, name, description, created_at)`、`PromptVersion(id, prompt_id, version, body, variables_json, created_at)`
  - `extract_vars(body: str) -> list[str]`（抽取去重排序的 `{{变量}}`）
  - `_get_owned(pid, user, session) -> Prompt`、`_latest(session, pid) -> PromptVersion`、`_detail(session, user, pid) -> dict`
  - 端点：`GET /api/prompts`、`POST /api/prompts`、`GET /api/prompts/{pid}`、`PUT /api/prompts/{pid}`、`DELETE /api/prompts/{pid}`
  - 列表项：`{id, name, description, latest_version, variables}`
  - 详情（**Task 1 暂无 used_by，Task 3 追加**）：`{id, name, description, current:{version, body, variables}, versions:[{version, created_at}]}`
  - 创建/更新 body：`{name, description, body}`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_prompts_api.py`

```python
import pytest


async def test_create_makes_v1_and_extracts_vars(auth_client):
    r = await auth_client.post("/api/prompts", json={"name": "问候", "description": "d", "body": "你好 {{name}} 与 {{name}} 和 {{age}}"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "问候" and d["current"]["version"] == 1
    assert d["current"]["body"] == "你好 {{name}} 与 {{name}} 和 {{age}}"
    assert d["current"]["variables"] == ["age", "name"]   # 去重排序


async def test_list_and_get(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x {{q}}"})).json()["id"]
    lst = (await auth_client.get("/api/prompts")).json()
    assert lst[0]["name"] == "P" and lst[0]["latest_version"] == 1 and lst[0]["variables"] == ["q"]
    d = (await auth_client.get(f"/api/prompts/{cid}")).json()
    assert d["current"]["body"] == "x {{q}}"


async def test_update_body_creates_new_version(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    d = (await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2 {{a}}"})).json()
    assert d["current"]["version"] == 2 and d["current"]["body"] == "v2 {{a}}"
    assert [v["version"] for v in d["versions"]] == [1, 2]


async def test_update_name_only_no_new_version(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "same"})).json()["id"]
    d = (await auth_client.put(f"/api/prompts/{cid}", json={"name": "P2", "description": "", "body": "same"})).json()
    assert d["name"] == "P2" and d["current"]["version"] == 1


async def test_delete(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    assert (await auth_client.delete(f"/api/prompts/{cid}")).status_code == 200
    assert (await auth_client.get(f"/api/prompts/{cid}")).status_code == 404


async def test_tenant_isolation(client):
    await client.post("/api/auth/login", json={"username": "u1"})
    cid = (await client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    await client.post("/api/auth/login", json={"username": "u2"})
    assert (await client.get(f"/api/prompts/{cid}")).status_code == 404
    assert (await client.put(f"/api/prompts/{cid}", json={"name": "x", "description": "", "body": "y"})).status_code == 404
    assert (await client.delete(f"/api/prompts/{cid}")).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py`
Expected: FAIL（404 / 路由不存在 / import error）

- [ ] **Step 3: 加两表** — `backend/app/models.py` 末尾追加

```python
class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id"), index=True)
    version: Mapped[int]
    body: Mapped[str] = mapped_column(Text, default="")
    variables_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

- [ ] **Step 4: 写 router** — `backend/app/routers/prompts.py`

```python
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.engine.nodes import TEMPLATE_RE   # 复用引擎占位符正则，保证抽取与渲染一致
from app.events import publish
from app.models import Prompt, PromptVersion, User

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptIn(BaseModel):
    name: str
    description: str = ""
    body: str = ""


def extract_vars(body: str) -> list[str]:
    return sorted({m.group(1) for m in TEMPLATE_RE.finditer(body or "")})


async def _get_owned(pid: int, user: User, session: AsyncSession) -> Prompt:
    p = await session.get(Prompt, pid)
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return p


async def _latest(session: AsyncSession, pid: int) -> PromptVersion:
    return (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version.desc()).limit(1))).scalar_one()


async def _detail(session: AsyncSession, user: User, pid: int) -> dict:
    p = await _get_owned(pid, user, session)
    vers = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version))).scalars().all()
    cur = vers[-1]
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "current": {"version": cur.version, "body": cur.body, "variables": json.loads(cur.variables_json)},
        "versions": [{"version": v.version, "created_at": v.created_at.isoformat()} for v in vers],
    }


@router.get("")
async def list_prompts(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ps = (await session.execute(select(Prompt).where(Prompt.user_id == user.id).order_by(Prompt.id))).scalars().all()
    out = []
    for p in ps:
        cur = await _latest(session, p.id)
        out.append({"id": p.id, "name": p.name, "description": p.description,
                    "latest_version": cur.version, "variables": json.loads(cur.variables_json)})
    return out


@router.post("")
async def create_prompt(body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = Prompt(user_id=user.id, name=body.name, description=body.description)
    session.add(p)
    await session.flush()
    session.add(PromptVersion(prompt_id=p.id, version=1, body=body.body,
                              variables_json=json.dumps(extract_vars(body.body), ensure_ascii=False)))
    await session.commit()
    publish(user.id, "prompt", p.id)
    return await _detail(session, user, p.id)


@router.get("/{pid}")
async def get_prompt(pid: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    return await _detail(session, user, pid)


@router.put("/{pid}")
async def update_prompt(pid: int, body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    p.name, p.description = body.name, body.description
    cur = await _latest(session, pid)
    if body.body != cur.body:   # 仅正文变化才追加新版本；名称/描述是元数据，原地改
        session.add(PromptVersion(prompt_id=pid, version=cur.version + 1, body=body.body,
                                  variables_json=json.dumps(extract_vars(body.body), ensure_ascii=False)))
    await session.commit()
    publish(user.id, "prompt", pid)
    return await _detail(session, user, pid)


@router.delete("/{pid}")
async def delete_prompt(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await session.execute(delete(PromptVersion).where(PromptVersion.prompt_id == pid))
    await session.delete(p)
    await session.commit()
    publish(user.id, "prompt", pid)
    return {"ok": True}
```

- [ ] **Step 5: 注册 router** — `backend/app/main.py`

第 11-12 行的 import 加 `prompts`：

```python
from app.routers import (admin, agent, auth, datasets, events, model_configs, model_logs,
                         prompts, runs, workflows)
```

第 29 行附近（`include_router(datasets.router)` 之后）加一行：

```python
    app.include_router(prompts.router)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py`
Expected: PASS（6 项）

- [ ] **Step 7: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/models.py backend/app/routers/prompts.py backend/app/main.py && git add -f backend/tests/test_prompts_api.py && git commit -m "feat(prompt): 提示词库数据模型 + CRUD API（正文版本化、变量抽取、租户隔离）"
```

---

### Task 2: 版本列表 + 回滚 + 复制端点

**Files:**
- Modify: `backend/app/routers/prompts.py`（追加 3 端点 + 2 个入参模型）
- Test: `backend/tests/test_prompts_api.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_get_owned` / `_latest` / `_detail` / `extract_vars`
- Produces:
  - `GET /api/prompts/{pid}/versions` → `[{version, body, variables, created_at}]`
  - `POST /api/prompts/{pid}/rollback` body `{version:int}` → 用该版本内容生成新版本 → 详情
  - `POST /api/prompts/{pid}/duplicate` body `{name?:str}` → 新建提示词（当前 body 为 v1）→ 详情

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_prompts_api.py`

```python
async def test_versions_lists_all(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2"})
    vs = (await auth_client.get(f"/api/prompts/{cid}/versions")).json()
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[0]["body"] == "v1" and vs[1]["body"] == "v2"


async def test_rollback_creates_new_version_with_old_body(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1 {{a}}"})).json()["id"]
    await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2"})
    d = (await auth_client.post(f"/api/prompts/{cid}/rollback", json={"version": 1})).json()
    assert d["current"]["version"] == 3 and d["current"]["body"] == "v1 {{a}}"
    assert d["current"]["variables"] == ["a"]


async def test_rollback_unknown_version_404(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    assert (await auth_client.post(f"/api/prompts/{cid}/rollback", json={"version": 9})).status_code == 404


async def test_duplicate_creates_new_prompt(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "原", "body": "正文 {{x}}"})).json()["id"]
    d = (await auth_client.post(f"/api/prompts/{cid}/duplicate", json={})).json()
    assert d["id"] != cid and d["name"] == "原 副本"
    assert d["current"]["version"] == 1 and d["current"]["body"] == "正文 {{x}}"
    named = (await auth_client.post(f"/api/prompts/{cid}/duplicate", json={"name": "自定义"})).json()
    assert named["name"] == "自定义"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py -k "versions or rollback or duplicate"`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 加端点** — `backend/app/routers/prompts.py` 末尾追加

```python
class RollbackIn(BaseModel):
    version: int


class DuplicateIn(BaseModel):
    name: str | None = None


@router.get("/{pid}/versions")
async def list_versions(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    await _get_owned(pid, user, session)
    vers = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version))).scalars().all()
    return [{"version": v.version, "body": v.body, "variables": json.loads(v.variables_json),
             "created_at": v.created_at.isoformat()} for v in vers]


@router.post("/{pid}/rollback")
async def rollback_prompt(pid: int, body: RollbackIn, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_owned(pid, user, session)
    target = (await session.execute(select(PromptVersion).where(
        PromptVersion.prompt_id == pid, PromptVersion.version == body.version))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    cur = await _latest(session, pid)
    session.add(PromptVersion(prompt_id=pid, version=cur.version + 1,
                              body=target.body, variables_json=target.variables_json))
    await session.commit()
    publish(user.id, "prompt", pid)
    return await _detail(session, user, pid)


@router.post("/{pid}/duplicate")
async def duplicate_prompt(pid: int, body: DuplicateIn, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    src = await _get_owned(pid, user, session)
    cur = await _latest(session, pid)
    new = Prompt(user_id=user.id, name=body.name or f"{src.name} 副本", description=src.description)
    session.add(new)
    await session.flush()
    session.add(PromptVersion(prompt_id=new.id, version=1, body=cur.body, variables_json=cur.variables_json))
    await session.commit()
    publish(user.id, "prompt", new.id)
    return await _detail(session, user, new.id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py`
Expected: PASS（10 项）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/routers/prompts.py && git add -f backend/tests/test_prompts_api.py && git commit -m "feat(prompt): 版本列表 + 回滚 + 复制端点"
```

---

### Task 3: 被引用扫描（detail 加 used_by）

**Files:**
- Modify: `backend/app/routers/prompts.py`（加 `_used_by` + `_detail` 注入 used_by + import Workflow）
- Test: `backend/tests/test_prompts_api.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_detail`
- Produces: `_used_by(session, user, pid) -> list[{workflow_id, workflow_name, node_id, slot}]`；`_detail` 返回新增 `used_by` 键

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_prompts_api.py`

```python
async def test_used_by_lists_referencing_nodes(auth_client):
    pid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    wf = (await auth_client.post("/api/workflows", json={"name": "流"})).json()
    graph = {"nodes": [{"id": "n1", "type": "llm_synth", "position": {"x": 0, "y": 0},
                        "config": {"system_prompt_ref": pid}}], "edges": []}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    d = (await auth_client.get(f"/api/prompts/{pid}")).json()
    assert d["used_by"] == [{"workflow_id": wf["id"], "workflow_name": "流", "node_id": "n1", "slot": "system_prompt"}]


async def test_used_by_empty_when_unreferenced(auth_client):
    pid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    assert (await auth_client.get(f"/api/prompts/{pid}")).json()["used_by"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py -k used_by`
Expected: FAIL（KeyError: 'used_by'）

- [ ] **Step 3: 实现** — `backend/app/routers/prompts.py`

import 行加 `Workflow`：

```python
from app.models import Prompt, PromptVersion, User, Workflow
```

在 `_detail` 之前加 helper：

```python
async def _used_by(session: AsyncSession, user: User, pid: int) -> list[dict]:
    wfs = (await session.execute(select(Workflow).where(Workflow.user_id == user.id))).scalars().all()
    out = []
    for wf in wfs:
        graph = json.loads(wf.graph_json)
        for node in graph.get("nodes", []):
            cfg = node.get("config", {})
            for slot in ("system_prompt", "user_prompt"):
                if cfg.get(f"{slot}_ref") == pid:
                    out.append({"workflow_id": wf.id, "workflow_name": wf.name,
                                "node_id": node["id"], "slot": slot})
    return out
```

`_detail` 的 return 字典加一行 `"used_by"`：

```python
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "current": {"version": cur.version, "body": cur.body, "variables": json.loads(cur.variables_json)},
        "versions": [{"version": v.version, "created_at": v.created_at.isoformat()} for v in vers],
        "used_by": await _used_by(session, user, pid),
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompts_api.py`
Expected: PASS（12 项）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/routers/prompts.py && git add -f backend/tests/test_prompts_api.py && git commit -m "feat(prompt): 详情含被引用节点列表（used_by 扫描工作流图）"
```

---

### Task 4: 引擎引用解析（runner，取最新版 / 缺失整 run 报错）

**Files:**
- Modify: `backend/app/engine/runner.py:11-12`（models import 加 Prompt, PromptVersion）、`:76-78`（解析调用）、文件加 `_resolve_prompt_refs` 函数
- Test: `backend/tests/test_prompt_resolve.py`

**Interfaces:**
- Consumes: Task 1 的 `Prompt`/`PromptVersion`
- Produces: `async def _resolve_prompt_refs(session_factory, graph, user_id) -> None`（原地把 `node.config["system_prompt"|"user_prompt"]` 填为引用提示词最新版 body；任一引用缺失抛 `ValueError`）

- [ ] **Step 1: 写失败测试** — `backend/tests/test_prompt_resolve.py`

```python
import json

import pytest
from sqlalchemy import select

from app.engine.graph import parse_graph
from app.engine.runner import _resolve_prompt_refs
from app.models import User


def _graph(config: dict) -> object:
    return parse_graph(json.dumps({
        "nodes": [{"id": "n1", "type": "llm_synth", "position": {"x": 0, "y": 0}, "config": config}],
        "edges": [],
    }))


async def _uid(session_factory) -> int:
    async with session_factory() as s:
        return (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()


async def test_resolve_injects_latest_body(auth_client, session_factory):
    p = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1 {{q}}"})).json()
    await auth_client.put(f"/api/prompts/{p['id']}", json={"name": "P", "description": "", "body": "v2 {{q}}"})
    graph = _graph({"system_prompt_ref": p["id"]})
    await _resolve_prompt_refs(session_factory, graph, await _uid(session_factory))
    assert graph.nodes[0].config["system_prompt"] == "v2 {{q}}"


async def test_resolve_missing_raises(auth_client, session_factory):
    graph = _graph({"user_prompt_ref": 99999})
    with pytest.raises(ValueError):
        await _resolve_prompt_refs(session_factory, graph, await _uid(session_factory))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_resolve.py`
Expected: FAIL（ImportError: _resolve_prompt_refs）

- [ ] **Step 3: 实现** — `backend/app/engine/runner.py`

models import（第 11-12 行）加 `Prompt, PromptVersion`：

```python
from app.models import (DatasetRow, ModelConfig, Prompt, PromptVersion, QcFailure, QcMetric, Run,
                        RunLog, RunNodeState, RunRow, WorkflowVersion)
```

加函数（放在 `_execute` 之前）：

```python
async def _resolve_prompt_refs(session_factory, graph, user_id: int) -> None:
    """run 启动解析：节点 system_prompt_ref/user_prompt_ref → 该提示词最新版 body 填入对应字段。
    任一引用缺失（不存在或非本人）抛 ValueError → execute_run 落 run.failed，不起跑节点。"""
    bodies: dict[int, str] = {}
    for node in graph.nodes:
        for slot in ("system_prompt", "user_prompt"):
            pid = node.config.get(f"{slot}_ref")
            if pid:
                bodies[pid] = ""
    if not bodies:
        return
    async with session_factory() as s:
        for pid in bodies:
            p = await s.get(Prompt, pid)
            if p is None or p.user_id != user_id:
                raise ValueError(f"引用的提示词 #{pid} 不存在")
            pv = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
                  .order_by(PromptVersion.version.desc()).limit(1))).scalar_one()
            bodies[pid] = pv.body
    for node in graph.nodes:
        for slot in ("system_prompt", "user_prompt"):
            pid = node.config.get(f"{slot}_ref")
            if pid:
                node.config[slot] = bodies[pid]
```

在 `_execute` 里 `validate_graph(graph)` 之后、`_log(... "运行开始")` 之前插一行（约第 77-78 行间）：

```python
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)
    await _resolve_prompt_refs(session_factory, graph, user_id)
    await _log(session_factory, run_id, "", "运行开始")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_resolve.py`
Expected: PASS（2 项）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/engine/runner.py && git add -f backend/tests/test_prompt_resolve.py && git commit -m "feat(prompt): run 启动解析提示词引用（取最新版 / 缺失整 run 报错）"
```

---

### Task 5: CLI `gf prompt` 全套

**Files:**
- Create: `backend/app/cli/commands/prompt.py`
- Modify: `backend/app/cli/__init__.py:26,29`（注册 prompt 模块）
- Modify: `backend/app/cli/client.py:16`（KIND_LABELS 加 prompts）
- Test: `backend/tests/test_prompt_cli.py`

**Interfaces:**
- Consumes: `Cli`、`die`、`cli.resolve("prompts", ref)`、commands/node.py 的 `_read_prompt(args)`
- Produces: `gf prompt ls / show / add / edit / rm / versions / rollback / dup`，`register(sub)`

- [ ] **Step 1: 写失败测试** — `backend/tests/test_prompt_cli.py`

```python
import app.cli as cli
from test_cli import gf, server   # 复用 server fixture 与 gf 包装


def _login(server):
    gf("login", "tester", "--server", server)


def test_prompt_add_and_ls(server, capsys, tmp_path):
    _login(server)
    f = tmp_path / "p.md"
    f.write_text("你好 {{name}}", encoding="utf-8")
    gf("prompt", "add", "问候", "--file", str(f), "--desc", "打招呼")
    capsys.readouterr()
    gf("prompt", "ls")
    assert "问候" in capsys.readouterr().out


def test_prompt_edit_creates_version(server, capsys, tmp_path):
    _login(server)
    f1 = tmp_path / "a.md"; f1.write_text("v1", encoding="utf-8")
    f2 = tmp_path / "b.md"; f2.write_text("v2", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f1))
    gf("prompt", "edit", "P", "--file", str(f2))
    capsys.readouterr()
    gf("prompt", "versions", "P")
    out = capsys.readouterr().out
    assert "v1" in out and "2" in out


def test_prompt_rollback_and_dup_and_rm(server, capsys, tmp_path):
    _login(server)
    f1 = tmp_path / "a.md"; f1.write_text("一", encoding="utf-8")
    f2 = tmp_path / "b.md"; f2.write_text("二", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f1))
    gf("prompt", "edit", "P", "--file", str(f2))
    gf("prompt", "rollback", "P", "1")
    gf("prompt", "dup", "P", "--name", "P2")
    capsys.readouterr()
    gf("prompt", "ls")
    out = capsys.readouterr().out
    assert "P" in out and "P2" in out
    gf("prompt", "rm", "P2")
    capsys.readouterr()
    gf("prompt", "ls")
    assert "P2" not in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_cli.py`
Expected: FAIL（argparse: invalid choice 'prompt'）

- [ ] **Step 3: KIND_LABELS 加 prompts** — `backend/app/cli/client.py` 第 16 行

```python
KIND_LABELS = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置", "prompts": "提示词"}
```

- [ ] **Step 4: 写 commands/prompt.py** — `backend/app/cli/commands/prompt.py`

```python
"""提示词库：prompt ls / show / add / edit / rm / versions / rollback / dup。"""
import json

from app.cli.client import Cli
from app.cli.commands.node import _read_prompt


def cmd_prompt_ls(args):
    cli = Cli()
    for p in cli.req("GET", "/api/prompts"):
        vs = "、".join(f"{{{{{v}}}}}" for v in p["variables"]) or "（无）"
        print(f"{p['id']:>4}  {p['name']}  v{p['latest_version']}  变量:{vs}  {p['description']}")


def cmd_prompt_show(args):
    cli = Cli()
    d = cli.req("GET", f"/api/prompts/{cli.resolve('prompts', args.ref)}")
    print(f"#{d['id']} {d['name']}（v{d['current']['version']}）  {d['description']}")
    print(f"变量: {'、'.join(d['current']['variables']) or '（无）'}")
    used = d.get("used_by", [])
    if used:
        print("被引用: " + "、".join(f"{u['workflow_name']}/{u['node_id']}({u['slot']})" for u in used))
    print("---\n" + d["current"]["body"])


def cmd_prompt_add(args):
    cli = Cli()
    d = cli.req("POST", "/api/prompts",
                json={"name": args.name, "description": args.desc or "", "body": _read_prompt(args)})
    print(f"已创建提示词 {d['name']}（#{d['id']}，v1，{len(d['current']['body'])} 字符）")


def cmd_prompt_edit(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    cur = cli.req("GET", f"/api/prompts/{pid}")
    payload = {"name": args.name or cur["name"],
               "description": args.desc if args.desc is not None else cur["description"],
               "body": _read_prompt(args)}
    d = cli.req("PUT", f"/api/prompts/{pid}", json=payload)
    print(f"已更新提示词 #{pid}（当前 v{d['current']['version']}）")


def cmd_prompt_rm(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    cli.req("DELETE", f"/api/prompts/{pid}")
    print(f"已删除提示词 #{pid}")


def cmd_prompt_versions(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    for v in cli.req("GET", f"/api/prompts/{pid}/versions"):
        head = v["body"].splitlines()[0] if v["body"] else ""
        print(f"v{v['version']}  {v['created_at'][:19]}  {head[:40]}")


def cmd_prompt_rollback(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    d = cli.req("POST", f"/api/prompts/{pid}/rollback", json={"version": args.version})
    print(f"已回滚提示词 #{pid} 到 v{args.version}（生成 v{d['current']['version']}）")


def cmd_prompt_dup(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    body = {"name": args.name} if args.name else {}
    d = cli.req("POST", f"/api/prompts/{pid}/duplicate", json=body)
    print(f"已复制为新提示词 {d['name']}（#{d['id']}）")


def register(sub):
    prompt = sub.add_parser("prompt", help="提示词库").add_subparsers(dest="action", required=True)
    s = prompt.add_parser("ls"); s.set_defaults(func=cmd_prompt_ls)
    s = prompt.add_parser("show"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_show)

    s = prompt.add_parser("add")
    s.add_argument("name"); s.add_argument("--desc")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file"); g.add_argument("--edit", action="store_true")
    g.add_argument("-", dest="from_stdin", action="store_true")
    s.set_defaults(func=cmd_prompt_add)

    s = prompt.add_parser("edit")
    s.add_argument("ref"); s.add_argument("--name"); s.add_argument("--desc")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file"); g.add_argument("--edit", action="store_true")
    g.add_argument("-", dest="from_stdin", action="store_true")
    s.set_defaults(func=cmd_prompt_edit)

    s = prompt.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_rm)
    s = prompt.add_parser("versions"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_versions)
    s = prompt.add_parser("rollback"); s.add_argument("ref"); s.add_argument("version", type=int); s.set_defaults(func=cmd_prompt_rollback)
    s = prompt.add_parser("dup"); s.add_argument("ref"); s.add_argument("--name"); s.set_defaults(func=cmd_prompt_dup)
```

> 注：`add`/`edit` 的 `--edit` 是「打开编辑器」开关（与 `_read_prompt` 一致），不是动作名；`_read_prompt` 读 `args.file`/`args.edit`/`args.from_stdin`，本表三选一与之对齐。

- [ ] **Step 5: 注册模块** — `backend/app/cli/__init__.py` 第 26、29 行

```python
    from app.cli.commands import auth, workflow, node, model, dataset, prompt, run
```

```python
    for mod in (auth, workflow, node, model, dataset, prompt, run):
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_cli.py`
Expected: PASS（3 项）

- [ ] **Step 7: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/cli/commands/prompt.py backend/app/cli/__init__.py backend/app/cli/client.py && git add -f backend/tests/test_prompt_cli.py && git commit -m "feat(prompt): gf prompt 全套命令（ls/show/add/edit/rm/versions/rollback/dup）"
```

---

### Task 6: CLI `gf node prompt --library`（引用/复制）

**Files:**
- Modify: `backend/app/cli/commands/node.py:91-98`（cmd_node_prompt 扩展）、`:132-141`（register 加参数）
- Test: `backend/tests/test_prompt_cli.py`（追加）

**Interfaces:**
- Consumes: Task 5 的 `cli.resolve("prompts", ...)`、Task 1 的 `GET /api/prompts/{id}` detail
- Produces: `gf node prompt <node> (--system|--user) --library <prompt> [--ref|--copy]`（默认 --copy）

- [ ] **Step 1: 写失败测试** — 追加到 `backend/tests/test_prompt_cli.py`

```python
def _wf_with_node(server):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流"); gf("use", "流"); gf("node", "add", "llm", "n1")


def test_node_prompt_library_ref(server, capsys, tmp_path):
    _wf_with_node(server)
    f = tmp_path / "p.md"; f.write_text("模板 {{q}}", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f))
    capsys.readouterr()
    gf("node", "prompt", "n1", "--system", "--library", "P", "--ref")
    assert "引用" in capsys.readouterr().out
    capsys.readouterr()
    gf("node", "show", "n1")
    assert "system_prompt_ref" in capsys.readouterr().out


def test_node_prompt_library_copy(server, capsys, tmp_path):
    _wf_with_node(server)
    f = tmp_path / "p.md"; f.write_text("正文内容 {{q}}", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f))
    capsys.readouterr()
    gf("node", "prompt", "n1", "--user", "--library", "P", "--copy")
    assert "复制" in capsys.readouterr().out
    capsys.readouterr()
    gf("node", "show", "n1")
    shown = capsys.readouterr().out
    assert "正文内容" in shown and "user_prompt_ref" not in shown
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_cli.py -k node_prompt`
Expected: FAIL（argparse: unrecognized arguments --library）

- [ ] **Step 3: 扩展 cmd_node_prompt** — `backend/app/cli/commands/node.py`

替换 `cmd_node_prompt`（第 91-98 行）：

```python
def cmd_node_prompt(args):
    cli = Cli()
    wf = cli.get_wf()
    node = find_node(wf["graph"], args.id)
    field = "system_prompt" if args.system else "user_prompt"
    cfg = node["config"]
    if args.library:
        pid = cli.resolve("prompts", args.library)
        if args.ref:
            cfg[f"{field}_ref"] = pid
            msg = f"已将 {args.id} 的 {field} 设为引用提示词 #{pid}（运行时取最新版）"
        else:   # 默认 copy：拉当前正文内联，并清除引用
            body = cli.req("GET", f"/api/prompts/{pid}")["current"]["body"]
            cfg[field] = body
            cfg.pop(f"{field}_ref", None)
            msg = f"已复制提示词 #{pid} 到 {args.id} 的 {field}（{len(body)} 字符）"
    else:
        cfg[field] = _read_prompt(args)
        cfg.pop(f"{field}_ref", None)   # 写内联即解除引用
        msg = f"已写入 {args.id} 的 {field}（{len(cfg[field])} 字符）"
    cli.put_graph(wf["id"], wf["graph"])
    print(msg)
```

替换 register 里 prompt 子解析器块（第 132-141 行），`--library` 进同一来源互斥组，新增 `--ref/--copy`：

```python
    s = node.add_parser("prompt")
    s.add_argument("id")
    g1 = s.add_mutually_exclusive_group(required=True)
    g1.add_argument("--system", action="store_true")
    g1.add_argument("--user", action="store_true")
    g2 = s.add_mutually_exclusive_group(required=True)
    g2.add_argument("--file")
    g2.add_argument("--edit", action="store_true")
    g2.add_argument("-", dest="from_stdin", action="store_true")
    g2.add_argument("--library", help="库提示词 id 或名")
    g3 = s.add_mutually_exclusive_group()
    g3.add_argument("--ref", action="store_true", help="引用（运行时取最新版）")
    g3.add_argument("--copy", action="store_true", help="复制当前正文进来（默认）")
    s.set_defaults(func=cmd_node_prompt)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_prompt_cli.py`
Expected: PASS（5 项）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow" && git add backend/app/cli/commands/node.py && git add -f backend/tests/test_prompt_cli.py && git commit -m "feat(prompt): gf node prompt --library 引用/复制库提示词"
```

---

### Task 7: 前端提示词库页 PromptsPage

**Files:**
- Create: `frontend/src/pages/PromptsPage.tsx`
- Create: `frontend/src/pages/PromptsPage.test.tsx`
- Modify: `frontend/src/api/types.ts`（加 Prompt 类型）
- Modify: `frontend/src/App.tsx`（import + 菜单项 + 路由）

**Interfaces:**
- Consumes: `api`（client.ts）、`useEvents`、react-markdown
- Produces: `PromptsPage` 默认导出；导出纯函数 `extractVars(body)`、`buildPromptPayload(v)`；类型 `PromptSummary`、`PromptDetail`

- [ ] **Step 1: 写失败测试** — `frontend/src/pages/PromptsPage.test.tsx`

```tsx
import { describe, expect, it } from 'vitest'
import { extractVars, buildPromptPayload } from './PromptsPage'

describe('PromptsPage helpers', () => {
  it('extracts unique sorted variables', () => {
    expect(extractVars('你好 {{name}} 与 {{name}} 和 {{age}}')).toEqual(['age', 'name'])
    expect(extractVars('无占位')).toEqual([])
  })

  it('builds payload trimming name', () => {
    expect(buildPromptPayload({ name: '  P  ', description: 'd', body: 'x' }))
      .toEqual({ name: 'P', description: 'd', body: 'x' })
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/pages/PromptsPage.test.tsx`
Expected: FAIL（无法解析 ./PromptsPage / 导出不存在）

- [ ] **Step 3: 加类型** — `frontend/src/api/types.ts` 末尾追加

```typescript
export interface PromptSummary {
  id: number; name: string; description: string; latest_version: number; variables: string[]
}
export interface PromptVersionMeta { version: number; created_at: string }
export interface PromptUsage { workflow_id: number; workflow_name: string; node_id: string; slot: string }
export interface PromptDetail {
  id: number; name: string; description: string
  current: { version: number; body: string; variables: string[] }
  versions: PromptVersionMeta[]
  used_by: PromptUsage[]
}
```

- [ ] **Step 4: 写页面** — `frontend/src/pages/PromptsPage.tsx`

```tsx
import { useEffect, useState } from 'react'
import { Button, Input, Popconfirm, Space, message } from 'antd'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import type { PromptDetail, PromptSummary } from '../api/types'
import { useEvents } from '../api/events'

const VAR_RE = /\{\{\s*([^{}]+?)\s*\}\}/g

export function extractVars(body: string): string[] {
  const set = new Set<string>()
  for (const m of (body ?? '').matchAll(VAR_RE)) set.add(m[1])
  return [...set].sort()
}

export function buildPromptPayload(v: { name: string; description: string; body: string }) {
  return { name: v.name.trim(), description: v.description ?? '', body: v.body ?? '' }
}

export default function PromptsPage() {
  const [list, setList] = useState<PromptSummary[]>([])
  const [sel, setSel] = useState<PromptDetail | null>(null)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [body, setBody] = useState('')
  const [search, setSearch] = useState('')

  const reload = () => api.get<PromptSummary[]>('/api/prompts').then(setList)
  useEffect(() => { void reload() }, [])
  useEvents((e) => { if (e.entity === 'prompt') void reload() })

  const openDetail = async (id: number) => {
    const d = await api.get<PromptDetail>(`/api/prompts/${id}`)
    setSel(d); setName(d.name); setDesc(d.description); setBody(d.current.body)
  }
  const openNew = () => { setSel(null); setName(''); setDesc(''); setBody('') }

  const save = async () => {
    const payload = buildPromptPayload({ name, description: desc, body })
    if (!payload.name) { message.error('请填写名称'); return }
    if (sel) { await api.put(`/api/prompts/${sel.id}`, payload); await openDetail(sel.id) }
    else { const d = await api.post<PromptDetail>('/api/prompts', payload); setSel(d) }
    await reload(); message.success('已保存')
  }
  const remove = async (id: number) => { await api.del(`/api/prompts/${id}`); setSel(null); await reload() }
  const duplicate = async (id: number) => {
    const d = await api.post<PromptDetail>(`/api/prompts/${id}/duplicate`, {})
    await reload(); await openDetail(d.id); message.success('已复制为新提示词')
  }
  const rollback = async (version: number) => {
    if (!sel) return
    await api.post(`/api/prompts/${sel.id}/rollback`, { version }); await openDetail(sel.id)
    message.success(`已回滚到 v${version}`)
  }

  const shown = list.filter((p) => p.name.includes(search))
  const vars = extractVars(body)

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 32px)' }}>
      <div style={{ width: 260, borderRight: '1px solid #eee', paddingRight: 12, overflow: 'auto' }}>
        <Button type="primary" size="small" onClick={openNew} style={{ marginBottom: 8 }}>新建</Button>
        <Input.Search placeholder="按名称搜索" value={search} onChange={(e) => setSearch(e.target.value)}
                      style={{ marginBottom: 8 }} />
        {shown.length === 0 && <div style={{ color: '#999' }}>还没有提示词，点「新建」</div>}
        {shown.map((p) => (
          <div key={p.id} onClick={() => void openDetail(p.id)}
               style={{ padding: 8, cursor: 'pointer', borderRadius: 4,
                        background: sel?.id === p.id ? '#e6f4ff' : undefined }}>
            <div style={{ fontWeight: 600 }}>{p.name}</div>
            <div style={{ color: '#999', fontSize: 12 }}>
              {p.description || '—'}　v{p.latest_version}　{p.variables.length} 变量
            </div>
          </div>
        ))}
      </div>
      <div style={{ flex: 1, overflow: 'auto' }}>
        <Space style={{ marginBottom: 8 }} wrap>
          <Input placeholder="名称" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 220 }} />
          <Input placeholder="描述" value={desc} onChange={(e) => setDesc(e.target.value)} style={{ width: 280 }} />
          <Button type="primary" onClick={() => void save()}>{sel ? '保存（新版本）' : '保存'}</Button>
          {sel && <Button onClick={() => void duplicate(sel.id)}>复制为新提示词</Button>}
          {sel && (
            <Popconfirm
              title={`确认删除？${sel.used_by.length ? `当前被 ${sel.used_by.length} 个节点引用，删后这些 run 会报错` : ''}`}
              onConfirm={() => void remove(sel.id)}>
              <Button danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
        <div style={{ display: 'flex', gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ color: '#666', marginBottom: 4 }}>正文（用 {'{{列名}}'} 引用数据列）</div>
            <Input.TextArea rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
            <div style={{ color: '#888', fontSize: 12, marginTop: 4 }}>
              声明变量：{vars.length ? vars.map((v) => `{{${v}}}`).join('、') : '（无）'}
            </div>
          </div>
          <div style={{ flex: 1, border: '1px solid #eee', borderRadius: 4, padding: 12, overflow: 'auto' }}>
            <ReactMarkdown>{body}</ReactMarkdown>
          </div>
        </div>
        {sel && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>版本历史</div>
            {sel.versions.slice().reverse().map((v) => (
              <Space key={v.version} style={{ display: 'flex', marginBottom: 4 }}>
                <span>v{v.version}</span>
                <span style={{ color: '#999' }}>{v.created_at.slice(0, 19)}</span>
                <a onClick={() => void rollback(v.version)}>回滚到此版</a>
              </Space>
            ))}
            {sel.used_by.length > 0 && (
              <div style={{ marginTop: 8, color: '#d4380d', fontSize: 12 }}>
                被引用：{sel.used_by.map((u) => `${u.workflow_name}/${u.node_id}(${u.slot})`).join('、')}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 5: 接入路由** — `frontend/src/App.tsx`

import 区加（其它 page import 旁）：

```tsx
import PromptsPage from './pages/PromptsPage'
```

菜单 items 里（`/models` 项之后）加：

```tsx
            { key: '/prompts', label: <Link to="/prompts">提示词库</Link> },
```

Routes 里（`/models` 路由之后）加：

```tsx
          <Route path="/prompts" element={<PromptsPage />} />
```

- [ ] **Step 6: 跑测试确认通过 + 构建校验**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/pages/PromptsPage.test.tsx`
Expected: PASS（2 项）

Run: `cd "E:/代码/GraphFlow/frontend" && npx tsc -b --noEmit`
Expected: 无类型错误

- [ ] **Step 7: 提交**

```bash
cd "E:/代码/GraphFlow" && git add frontend/src/pages/PromptsPage.tsx frontend/src/pages/PromptsPage.test.tsx frontend/src/api/types.ts frontend/src/App.tsx && git commit -m "feat(prompt): 提示词库管理页（左列表+右分栏 md 预览+版本回滚+复制+删除护栏）"
```

---

### Task 8: 节点表单「从库」复制/引用 + 变量缺失提示

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（加 import、`missingLibVars`、`LibraryPromptControl`，在 llm/qc 两处提示词区插入控件）
- Create: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`

**Interfaces:**
- Consumes: `api`、`PromptSummary`/`PromptDetail`、表单内 `patch`、`inputCols`
- Produces: 导出 `missingLibVars(promptVars, inputCols)`；`LibraryPromptControl` 控件

- [ ] **Step 1: 写失败测试** — `frontend/src/canvas/forms/NodeConfigForm.test.tsx`

```tsx
import { describe, expect, it } from 'vitest'
import { missingLibVars } from './NodeConfigForm'

describe('missingLibVars', () => {
  it('returns prompt vars not present in input columns', () => {
    expect(missingLibVars(['q', 'a'], ['q'])).toEqual(['a'])
    expect(missingLibVars(['q'], ['q', 'a'])).toEqual([])
    expect(missingLibVars([], ['q'])).toEqual([])
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx`
Expected: FAIL（missingLibVars 未导出）

- [ ] **Step 3: 实现** — `frontend/src/canvas/forms/NodeConfigForm.tsx`

确认顶部已 import `useState, useEffect`（React）；补 import：

```tsx
import { api } from '../../api/client'
import type { PromptDetail, PromptSummary } from '../../api/types'
```

（若已有 `import type { ... } from '../../api/types'` 行，把 `PromptDetail, PromptSummary` 并入即可。）

在 `MissingColsWarning`（约第 98 行）之后加 helper + 控件：

```tsx
export function missingLibVars(promptVars: string[], inputCols: string[]): string[] {
  return promptVars.filter((v) => !inputCols.includes(v))
}

function LibraryPromptControl({ slot, config, patch, inputCols }: {
  slot: 'system_prompt' | 'user_prompt'
  config: Record<string, any>
  patch: (p: object) => void
  inputCols: string[]
}) {
  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [pick, setPick] = useState<number | undefined>(undefined)
  useEffect(() => { void api.get<PromptSummary[]>('/api/prompts').then(setPrompts) }, [])
  const refId = config[`${slot}_ref`] as number | undefined
  const refName = prompts.find((p) => p.id === refId)?.name
  const picked = prompts.find((p) => p.id === pick)
  const miss = picked ? missingLibVars(picked.variables, inputCols) : []
  const copy = async () => {
    if (!pick) return
    const d = await api.get<PromptDetail>(`/api/prompts/${pick}`)
    patch({ [slot]: d.current.body, [`${slot}_ref`]: undefined })
  }
  const ref = () => { if (pick) patch({ [`${slot}_ref`]: pick }) }
  const unref = () => patch({ [`${slot}_ref`]: undefined })
  return (
    <div style={{ marginBottom: 6 }}>
      {refId ? (
        <div style={{ fontSize: 12, color: '#1677ff' }}>
          引用库提示词：{refName ?? `#${refId}`}（运行时取最新版）
          <a style={{ marginLeft: 8 }} onClick={unref}>解除引用</a>
        </div>
      ) : (
        <Space size={4} wrap>
          <span style={{ fontSize: 12, color: '#999' }}>从库：</span>
          <Select size="small" style={{ width: 160 }} placeholder="选择提示词" value={pick}
                  onChange={setPick}
                  options={prompts.map((p) => ({ value: p.id, label: p.name }))} />
          <a onClick={() => void copy()}>复制进来</a>
          <a onClick={ref}>引用</a>
          {miss.length > 0 && (
            <span style={{ color: '#d4380d', fontSize: 12 }}>
              缺列：{miss.map((c) => `{{${c}}}`).join('、')}
            </span>
          )}
        </Space>
      )}
    </div>
  )
}
```

在 llm_synth 提示词区（约第 296-307 行）的 System / User 两个 Field 内、各自 TextArea 之前插入控件。System 字段：

```tsx
            <Field label="System Prompt">
              <LibraryPromptControl slot="system_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                              onChange={(e) => patch({ system_prompt: e.target.value })} />
            </Field>
```

User 字段：

```tsx
            <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
              <LibraryPromptControl slot="user_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={6} value={config.user_prompt ?? ''}
                              onChange={(e) => patch({ user_prompt: e.target.value })} />
              <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
            </Field>
```

在 qc 提示词区（约第 560-569 行）做同样插入（System 用 `slot="system_prompt"`、User 用 `slot="user_prompt"`，`config`/`patch`/`inputCols` 同名在 qc 表单内同样在作用域）。

> 实现者注意：插入前先确认所在表单函数内 `patch` 与 `inputCols` 的实际变量名（llm 区是 `patch`/`inputCols`）。qc 表单若 `patch` 名称不同，按该表单实际的 patch 闭包传入。

- [ ] **Step 4: 跑测试确认通过 + 构建校验**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx`
Expected: PASS（1 项）

Run: `cd "E:/代码/GraphFlow/frontend" && npx tsc -b --noEmit`
Expected: 无类型错误

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow" && git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx && git commit -m "feat(prompt): 节点表单从库复制/引用提示词 + 变量缺失提示"
```

---

### Task 9: gf-prompt 技能 + 总入口路由 + gf-node-prompt 补注

**Files:**
- Create: `.claude/skills/gf-prompt/SKILL.md`
- Modify: `.claude/skills/gf-cli/SKILL.md`（路由表加一行）
- Modify: `.claude/skills/gf-node-prompt/SKILL.md`（node prompt 段加 --library 说明）

**Interfaces:** 无代码接口；文档与已实现命令一致。

- [ ] **Step 1: 写 gf-prompt 技能** — `.claude/skills/gf-prompt/SKILL.md`

```markdown
---
name: gf-prompt
description: Use when 用 gf 管理可复用提示词库——`prompt ls/show/add/edit/rm` 维护提示词、`prompt versions/rollback` 看版本与回滚、`prompt dup` 复制，以及把库提示词通过 `node prompt --library` 引用/复制到节点；遇到「正文变更才生成新版本」「引用取最新版/缺失整 run 报错」「ref 与 copy 区别」「声明变量 {{x}}」相关疑问
---

# gf-prompt —— 可复用提示词库

提示词库是**每用户私有**的命名提示词集合，可版本化、复用到工作流节点。前置 `gf login`。

## 命令

```powershell
gf prompt ls                               # 列出：#id 名称 v最新版 变量 描述
gf prompt show <id|名>                      # 当前正文 + 声明变量 + 被引用节点
gf prompt add <名称> --file p.md [--desc 说明]   # 新建（建 v1）；正文也可 --edit / -（stdin）
gf prompt edit <id|名> --file p.md [--name 新名] [--desc 新说明]   # 改正文（正文变了才出新版本）
gf prompt versions <id|名>                  # 列所有版本
gf prompt rollback <id|名> <版本号>          # 回滚：用旧版内容生成新版本（线性无损）
gf prompt dup <id|名> [--name 新名]          # 复制为新提示词（默认名「<原名> 副本」）
gf prompt rm <id|名>                         # 删除（删后引用它的 run 会报错，见下）
```

- 正文来源三选一：`--file FILE` / `--edit`（开 $EDITOR）/ `-`（stdin），与 `node prompt` 一致。
- 资源指代：纯数字按 id，否则按名精确匹配，重名报候选（同其它资源）。

## 关键契约（照此理解，别猜）

- **版本**：只有**正文 body 变化**才追加新版本；名称/描述是元数据，原地改不出版本。回滚不覆盖历史，而是用旧内容生成新版本。
- **声明变量**：保存时自动抽取正文里的 `{{变量}}`（与节点渲染同款），`show`/`ls` 显示。提示词正文里只写列名占位符，不写行值。
- **被引用**:`show` 列出哪些工作流节点引用了它；删除被引用的提示词后，那些 run 启动解析时会**整 run 报错**（fail fast）。

## 把库提示词用到节点（gf node prompt --library）

```powershell
gf node prompt <节点> --system --library <提示词> --ref    # 引用：节点存 *_ref，运行时取最新版
gf node prompt <节点> --user   --library <提示词> --copy   # 复制：把当前正文内联进节点（默认）
```

- `--ref`：节点 config 写 `system_prompt_ref`/`user_prompt_ref`=提示词 id；**改库即影响所有引用节点**；引用缺失则整 run 报错。
- `--copy`（默认）：拉当前正文写进 `system_prompt`/`user_prompt` 内联字段，之后与库独立；并清除该槽位的引用。
- 写内联文本（`--file/--edit/-`，不带 `--library`）也会清除该槽位的引用。

详见 gf-node-prompt 的 `node prompt` 段。
```

- [ ] **Step 2: 总入口路由表加一行** — `.claude/skills/gf-cli/SKILL.md`

在路由表（`gf-run` 行之后）加：

```markdown
| 管理可复用提示词库（库 CRUD、版本、回滚、复制、被引用、引用到节点） | **gf-prompt** | `prompt ls/show/add/edit/rm/versions/rollback/dup` `node prompt --library` |
```

- [ ] **Step 3: gf-node-prompt 补 --library 说明** — `.claude/skills/gf-node-prompt/SKILL.md`

在 `## gf node prompt ...` 段（约第 56 行「必须二选一指定来源」之后）追加：

```markdown
- 也可不写文本、改从**提示词库**取：`--library <提示词id或名>` 配 `--ref`（引用，运行时取最新版）或 `--copy`（复制当前正文进来，默认）。引用写 `system_prompt_ref`/`user_prompt_ref`，运行时解析；缺失则整 run 报错。库的维护见 **gf-prompt**。
```

- [ ] **Step 4: 校验文档与命令一致**

Run: `cd "E:/代码/GraphFlow" && grep -n "gf-prompt" .claude/skills/gf-cli/SKILL.md && grep -n "library" .claude/skills/gf-node-prompt/SKILL.md`
Expected: 各至少一处命中

- [ ] **Step 5: 全量后端回归 + 提交**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`
Expected: 全绿（含新增 test_prompts_api / test_prompt_resolve / test_prompt_cli）

```bash
cd "E:/代码/GraphFlow" && git add .claude/skills/gf-prompt/SKILL.md .claude/skills/gf-cli/SKILL.md .claude/skills/gf-node-prompt/SKILL.md && git commit -m "docs(prompt): gf-prompt 技能 + 总入口路由 + gf-node-prompt 补 --library"
```

---

## 完成后

全部 9 任务完成后用 superpowers:finishing-a-development-branch 收尾（合并 master 并删分支，不推 origin）。
合并前跑全量后端回归 + 前端 `npx vitest run` 全绿。
