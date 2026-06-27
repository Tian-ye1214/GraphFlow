# Agent 全生命周期工具 Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 run/model/dataset/prompt 写操作 + restore + workflow 导入导出做成主 RedLotus Agent 的细粒度 pydantic-ai 工具（26 个），补齐全生命周期。

**Architecture:** 沿用 Phase 1 三层：REST 内联的写逻辑抽成 service 单点（REST + Agent 共用，零行为漂移靠既有测试守）→ 4 个新 toolkit（RunToolkit/ModelToolkit/DatasetToolkit/PromptToolkit）+ GraphToolkit 追加导入导出 → `system.py:_make_tools` 装配并透传 `confirm_delete`。破坏性工具（5 删除 + restore）走 Phase 1 复审补修立的 confirm_delete 门禁；model 写工具 api_key 在渲染单点 `_brief` 打码。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / pydantic-ai / pytest（异步用例自动 await，session_factory 夹具见 `backend/tests/conftest.py`）。

## Global Constraints

- 后端测试在 `backend/` 下跑：`cd backend && python -m pytest`（Windows PowerShell：`$env:PYTHONIOENCODING="utf-8"`）。
- 工具体**绝不抛异常进 pydantic-ai 框架**：catch 后返回人话 `Error: …` 串（对齐 Phase 1 `_mutate` / columns 端点 422 降级）。归属不符返回「<资源>不存在」串。
- 破坏性工具门禁范式（已立于 `graph_tools.py` GraphToolkit.delete_workflow）：toolkit 构造收 `confirm_delete: bool = False`；未确认时返回需确认串（含 `[confirm_delete] <gf 等价命令或描述>`）**不执行**；用户回「确认」开头消息→`turns.py:157` confirm_delete=True→当回合执行。
- **复用优先/单点化/KISS/不预防未发生的 bug**：抽取后 REST 路由改 delegate，不留重复逻辑；不加投机抽象/额度护栏/假运行。
- 读工具结果走 `app.agent.data_preview._fit_budget` 防爆 wrap_tools 20k 截断。
- 跨租户：每个工具先按 `(资源, user_id)` 归属校验，他人 id 一律返回「不存在」串、绝不改数据。
- 文件路径工具用现成沙箱解析器 `resolve_in(workdir, rel_path)`（越界抛 ValueError，见 `app/agent/tools.py` 被 read_file 复用），不自造路径拼接。
- 提交信息中文、不出现 claude、不加 Co-Authored-By。`backend/tests/` 本地跟踪、不推 origin。

## File Structure

**新建 service：**
- `backend/app/services/model_service.py` — create/update/delete 模型（加密收口）
- `backend/app/services/prompt_service.py` — create/update/delete/rollback/duplicate 提示词（版本化收口）
- `backend/app/services/dataset_service.py` — delete 数据集（级联收口）+ ingest_file（上传摄入收口）

**新建 toolkit：**
- `backend/app/agent/run_tools.py` — `RunToolkit`
- `backend/app/agent/model_tools.py` — `ModelToolkit`
- `backend/app/agent/dataset_tools.py` — `DatasetToolkit`
- `backend/app/agent/prompt_tools.py` — `PromptToolkit`

**修改：**
- `backend/app/agent/tools.py` — `_brief` 增密钥打码
- `backend/app/services/run_service.py` — 加 `restore_workflow_from_run`
- `backend/app/services/workflow_package.py` — 不改（复用 export_package/import_package）
- `backend/app/agent/graph_tools.py` — GraphToolkit 加 `export_workflow`/`import_workflow`（构造加 `workdir`）
- `backend/app/agent/system.py` — `_make_tools` 装配 4 新 toolkit + 传 workdir 给 Graph/Dataset toolkit
- `backend/app/routers/runs.py` — restore delegate 到 service
- `backend/app/routers/model_configs.py` — create/update/delete delegate
- `backend/app/routers/prompts.py` — create/update/delete/rollback/duplicate delegate
- `backend/app/routers/datasets.py` — upload 每文件体 + delete delegate

**测试：**
- `backend/tests/test_model_service.py`、`test_prompt_service.py`、`test_dataset_service.py`（新）
- `backend/tests/test_run_tools.py`、`test_model_tools.py`、`test_dataset_tools.py`、`test_prompt_tools.py`（新）
- `backend/tests/test_graph_tools.py`、`test_agent_system.py`（扩）
- `backend/tests/test_agent_tools.py`（`_brief` 打码；若无则新建）
- 既有 `test_model_configs.py`/`test_prompts.py`/`test_datasets.py`/`test_runs.py` 守 REST delegate 后行为不变

---

### Task 1: `_brief` 密钥渲染打码

**Files:**
- Modify: `backend/app/agent/tools.py:40-43`（`_brief`）
- Test: `backend/tests/test_agent_tools.py`

**Interfaces:**
- Produces: `_brief(kwargs: dict) -> str`，对名 ∈ {api_key, key, token, secret, password} 的 kwarg 值渲成 `***`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_agent_tools.py
from app.agent.tools import _brief


def test_brief_redacts_secret_named_kwargs():
    out = _brief({"name": "gpt", "api_key": "sk-supersecret-xyz", "base_url": "http://x"})
    assert "sk-supersecret" not in out
    assert "api_key=***" in out
    assert "gpt" in out          # 非密钥参数照常显示


def test_brief_keeps_command_branch():
    assert _brief({"command": "gf runs"}) == "gf runs"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_tools.py -q`
Expected: FAIL（`api_key=sk-supersecret...` 未打码）

- [ ] **Step 3: 实现打码**

```python
# backend/app/agent/tools.py 替换 _brief
_SECRET_KEYS = {"api_key", "key", "token", "secret", "password"}


def _brief(kwargs: dict) -> str:
    if "command" in kwargs:
        return str(kwargs["command"])[:80]
    parts = []
    for k, v in list(kwargs.items())[:3]:
        shown = "***" if k.lower() in _SECRET_KEYS else str(v)[:40]
        parts.append(f"{k}={shown}")
    return ", ".join(parts)[:80]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_agent_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/tools.py backend/tests/test_agent_tools.py
git commit -m "feat(agent): _brief 渲染单点对密钥名参数打码"
```

---

### Task 2: restore_workflow_from_run 抽成 service + runs.py delegate

**Files:**
- Modify: `backend/app/services/run_service.py`（加函数）
- Modify: `backend/app/routers/runs.py:241-252`（delegate）
- Test: `backend/tests/test_run_service.py`（新）、既有 `tests/test_runs.py` 守不变

**Interfaces:**
- Produces: `async restore_workflow_from_run(session, run, user_id) -> Workflow | None`（run 的版本图覆盖工作流；他人工作流→None；成功 commit+publish 并返回 wf）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_run_service.py
import json
from app.models import Run, User, Workflow, WorkflowVersion
from app.services.run_service import restore_workflow_from_run


async def _seed_run_with_version(sf, old_graph, ver_graph):
    async with sf() as s:
        u = User(username="ru"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(old_graph))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(ver_graph))
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.commit()
        return u.id, wf.id, run.id


async def test_restore_overwrites_workflow_graph(session_factory):
    sf = session_factory
    uid, wf_id, run_id = await _seed_run_with_version(
        sf, {"nodes": [], "edges": []}, {"nodes": [{"id": "a", "type": "input", "config": {}}], "edges": []})
    async with sf() as s:
        run = await s.get(Run, run_id)
        wf = await restore_workflow_from_run(s, run, uid)
        assert wf is not None
    async with sf() as s:
        g = json.loads((await s.get(Workflow, wf_id)).graph_json)
        assert [n["id"] for n in g["nodes"]] == ["a"]


async def test_restore_cross_tenant_returns_none(session_factory):
    sf = session_factory
    uid, wf_id, run_id = await _seed_run_with_version(sf, {"nodes": [], "edges": []}, {"nodes": [], "edges": []})
    async with sf() as s:
        run = await s.get(Run, run_id)
        assert await restore_workflow_from_run(s, run, uid + 999) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_run_service.py -q`
Expected: FAIL（`ImportError: cannot import name 'restore_workflow_from_run'`）

- [ ] **Step 3: 实现 service + router delegate**

```python
# backend/app/services/run_service.py 追加（顶部已 import Workflow, WorkflowVersion）
from app.events import publish   # 若未导入则加


async def restore_workflow_from_run(session, run, user_id: int):
    """把 run 快照版本的图覆盖回工作流。他人工作流返回 None；成功 commit + 发 workflow 事件，返回 wf。
    REST 路由与 Agent restore_workflow_from_run 工具共用此单点。"""
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    if wf is None or wf.user_id != user_id:
        return None
    wf.graph_json = ver.graph_json
    await session.commit()
    publish(user_id, "workflow", wf.id)
    return wf
```

```python
# backend/app/routers/runs.py 替换 restore_run_version 体（241-252）
@router.post("/{run_id}/restore")
async def restore_run_version(run_id: int, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    wf = await restore_workflow_from_run(session, run, user.id)
    if wf is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return {"ok": True}
```

在 `runs.py` 顶部 import 加：`from app.services.run_service import restore_workflow_from_run`（与既有 run_service import 合并）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_run_service.py tests/test_runs.py -q`
Expected: PASS（含既有 restore REST 回归）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/run_service.py backend/app/routers/runs.py backend/tests/test_run_service.py
git commit -m "refactor(run): restore_workflow_from_run 抽 service 单点 REST+Agent 共用"
```

---

### Task 3: model_service 抽取 + model_configs.py delegate

**Files:**
- Create: `backend/app/services/model_service.py`
- Modify: `backend/app/routers/model_configs.py:75-113`（create/update/delete delegate）
- Test: `backend/tests/test_model_service.py`（新）、既有 `tests/test_model_configs.py` 守不变

**Interfaces:**
- Consumes: `crypto.encrypt`（crypto.py:14）。
- Produces:
  - `async create_model(session, user_id, *, name, model_name, base_url, provider="openai", azure_api_mode="legacy", api_version="", api_key="", default_params=None) -> ModelConfig`（加密 api_key；commit+publish）
  - `async update_model(session, mc, *, name, model_name, base_url, provider, azure_api_mode, api_version, default_params, api_key="") -> ModelConfig`（api_key 留空=不改；commit+publish）
  - `async delete_model(session, mc) -> None`（delete+commit+publish）

> 注：provider 字段校验（Azure 必填 api_version/api_key）保留在 router 的 `_validated_provider_fields`（HTTP 语义），不进 service。service 只做「落库+加密+事件」。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_model_service.py
from app import crypto
from app.models import ModelConfig, User
from app.services import model_service


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="mu"); s.add(u); await s.commit(); return u.id


async def test_create_model_encrypts_key(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(
            s, uid, name="m", model_name="gpt", base_url="http://x", api_key="sk-secret")
        mc_id = mc.id
    async with sf() as s:
        got = await s.get(ModelConfig, mc_id)
        assert got.api_key_enc and got.api_key_enc != "sk-secret"   # 已加密
        assert crypto.decrypt(got.api_key_enc) == "sk-secret"


async def test_update_model_blank_key_keeps_existing(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(s, uid, name="m", model_name="g", base_url="u", api_key="sk-1")
        enc1 = mc.id
    async with sf() as s:
        mc = await s.get(ModelConfig, enc1)
        before = mc.api_key_enc
        await model_service.update_model(s, mc, name="m2", model_name="g", base_url="u",
                                         provider="openai", azure_api_mode="legacy",
                                         api_version="", default_params={}, api_key="")
    async with sf() as s:
        got = await s.get(ModelConfig, enc1)
        assert got.name == "m2" and got.api_key_enc == before   # 名改了、密钥未动


async def test_delete_model(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(s, uid, name="m", model_name="g", base_url="u")
        mid = mc.id
        await model_service.delete_model(s, mc)
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_model_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 service + router delegate**

```python
# backend/app/services/model_service.py
"""模型配置写入服务单点：REST 路由与 Agent ModelToolkit 共用，密钥加密收口于此。"""
import json

from app import crypto
from app.events import publish
from app.models import ModelConfig


async def create_model(session, user_id: int, *, name: str, model_name: str, base_url: str,
                       provider: str = "openai", azure_api_mode: str = "legacy",
                       api_version: str = "", api_key: str = "",
                       default_params: dict | None = None) -> ModelConfig:
    mc = ModelConfig(
        user_id=user_id, name=name, model_name=model_name, base_url=base_url,
        provider=provider, azure_api_mode=azure_api_mode, api_version=api_version,
        api_key_enc=crypto.encrypt(api_key) if api_key else "",
        default_params_json=json.dumps(default_params or {}, ensure_ascii=False))
    session.add(mc)
    await session.commit()
    publish(user_id, "model", mc.id)
    return mc


async def update_model(session, mc: ModelConfig, *, name: str, model_name: str, base_url: str,
                       provider: str, azure_api_mode: str, api_version: str,
                       default_params: dict, api_key: str = "") -> ModelConfig:
    mc.name, mc.model_name, mc.base_url = name, model_name, base_url
    mc.provider, mc.azure_api_mode, mc.api_version = provider, azure_api_mode, api_version
    mc.default_params_json = json.dumps(default_params, ensure_ascii=False)
    if api_key:                       # 留空=不改既有密钥
        mc.api_key_enc = crypto.encrypt(api_key)
    await session.commit()
    publish(mc.user_id, "model", mc.id)
    return mc


async def delete_model(session, mc: ModelConfig) -> None:
    uid, mid = mc.user_id, mc.id
    await session.delete(mc)
    await session.commit()
    publish(uid, "model", mid)
```

```python
# backend/app/routers/model_configs.py 改 create/update/delete 体 delegate（保留 _validated_provider_fields 校验）
from app.services import model_service   # 顶部加

@router.post("")
async def create_model(body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    provider, api_version, azure_api_mode = _validated_provider_fields(body)
    mc = await model_service.create_model(
        session, user.id, name=body.name, model_name=body.model_name, base_url=body.base_url,
        provider=provider, azure_api_mode=azure_api_mode, api_version=api_version,
        api_key=body.api_key, default_params=body.default_params)
    return _out(mc)


@router.put("/{mc_id}")
async def update_model(mc_id: int, body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    provider, api_version, azure_api_mode = _validated_provider_fields(body, existing_key=mc.api_key_enc)
    mc = await model_service.update_model(
        session, mc, name=body.name, model_name=body.model_name, base_url=body.base_url,
        provider=provider, azure_api_mode=azure_api_mode, api_version=api_version,
        default_params=body.default_params, api_key=body.api_key)
    return _out(mc)


@router.delete("/{mc_id}")
async def delete_model(mc_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    await model_service.delete_model(session, mc)
    return {"ok": True}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_model_service.py tests/test_model_configs.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/model_service.py backend/app/routers/model_configs.py backend/tests/test_model_service.py
git commit -m "refactor(model): create/update/delete 抽 model_service 单点 REST+Agent 共用"
```

---

### Task 4: prompt_service 抽取 + prompts.py delegate

**Files:**
- Create: `backend/app/services/prompt_service.py`
- Modify: `backend/app/routers/prompts.py:83-172`（create/update/delete/rollback/duplicate delegate）
- Test: `backend/tests/test_prompt_service.py`（新）、既有 `tests/test_prompts.py` 守不变

**Interfaces:**
- Consumes: `extract_vars`（prompts.py:23 —— 移入 service，router re-import）、`_latest`（prompts.py:34）。
- Produces（均 commit + publish，返回主键便于 router `_detail` 复算）：
  - `async create_prompt(session, user_id, *, name, description="", body="") -> int`
  - `async update_prompt(session, prompt, *, name, description, body) -> None`（正文变更才出新版）
  - `async delete_prompt(session, prompt) -> None`（级联删 PromptVersion）
  - `async rollback_prompt(session, prompt_id, version) -> bool`（目标版不存在返 False）
  - `async duplicate_prompt(session, src, new_name=None) -> int`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_prompt_service.py
import json
from app.models import Prompt, PromptVersion, User
from app.services import prompt_service
from sqlalchemy import func, select


async def _seed(sf):
    async with sf() as s:
        u = User(username="pu"); s.add(u); await s.commit(); return u.id


async def _versions(sf, pid):
    async with sf() as s:
        return (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
                .order_by(PromptVersion.version))).scalars().all()


async def test_create_prompt_makes_v1_and_extracts_vars(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="你好 {{q}} 和 {{name}}")
    vers = await _versions(sf, pid)
    assert len(vers) == 1 and vers[0].version == 1
    assert json.loads(vers[0].variables_json) == ["name", "q"]


async def test_update_prompt_new_version_only_on_body_change(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="v1")
    async with sf() as s:
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="改名", description="d", body="v1")  # 正文没变
    assert len(await _versions(sf, pid)) == 1
    async with sf() as s:
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="改名", description="d", body="v2")  # 正文变
    assert len(await _versions(sf, pid)) == 2


async def test_rollback_copies_old_body_as_new_version(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="老")
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="p", description="", body="新")
    async with sf() as s:
        assert await prompt_service.rollback_prompt(s, pid, 1) is True
    vers = await _versions(sf, pid)
    assert len(vers) == 3 and vers[-1].body == "老"
    async with sf() as s:
        assert await prompt_service.rollback_prompt(s, pid, 999) is False


async def test_delete_prompt_cascades_versions(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="x")
        p = await s.get(Prompt, pid)
        await prompt_service.delete_prompt(s, p)
    async with sf() as s:
        assert await s.get(Prompt, pid) is None
        cnt = (await s.execute(select(func.count()).select_from(PromptVersion)
               .where(PromptVersion.prompt_id == pid))).scalar()
        assert cnt == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_prompt_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 service + router delegate**

```python
# backend/app/services/prompt_service.py
"""提示词写入服务单点：版本化语义（正文变更才出新版）收口于此，REST 与 Agent PromptToolkit 共用。"""
import json

from sqlalchemy import delete as sa_delete, select

from app.engine.nodes import TEMPLATE_RE
from app.events import publish
from app.models import Prompt, PromptVersion


def extract_vars(body: str) -> list[str]:
    return sorted({m.group(1) for m in TEMPLATE_RE.finditer(body or "")})


async def _latest(session, pid: int) -> PromptVersion:
    return (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version.desc()).limit(1))).scalar_one()


async def create_prompt(session, user_id: int, *, name: str, description: str = "",
                        body: str = "") -> int:
    p = Prompt(user_id=user_id, name=name, description=description)
    session.add(p)
    await session.flush()
    session.add(PromptVersion(prompt_id=p.id, version=1, body=body,
                              variables_json=json.dumps(extract_vars(body), ensure_ascii=False)))
    await session.commit()
    publish(user_id, "prompt", p.id)
    return p.id


async def update_prompt(session, prompt: Prompt, *, name: str, description: str, body: str) -> None:
    prompt.name, prompt.description = name, description
    cur = await _latest(session, prompt.id)
    if body != cur.body:    # 仅正文变化才追加新版本；名/描述原地改
        session.add(PromptVersion(prompt_id=prompt.id, version=cur.version + 1, body=body,
                                  variables_json=json.dumps(extract_vars(body), ensure_ascii=False)))
    await session.commit()
    publish(prompt.user_id, "prompt", prompt.id)


async def delete_prompt(session, prompt: Prompt) -> None:
    uid, pid = prompt.user_id, prompt.id
    await session.execute(sa_delete(PromptVersion).where(PromptVersion.prompt_id == pid))
    await session.delete(prompt)
    await session.commit()
    publish(uid, "prompt", pid)


async def rollback_prompt(session, prompt_id: int, version: int) -> bool:
    target = (await session.execute(select(PromptVersion).where(
        PromptVersion.prompt_id == prompt_id, PromptVersion.version == version))).scalar_one_or_none()
    if target is None:
        return False
    cur = await _latest(session, prompt_id)
    session.add(PromptVersion(prompt_id=prompt_id, version=cur.version + 1,
                              body=target.body, variables_json=target.variables_json))
    await session.commit()
    p = await session.get(Prompt, prompt_id)
    publish(p.user_id, "prompt", prompt_id)
    return True


async def duplicate_prompt(session, src: Prompt, new_name: str | None = None) -> int:
    cur = await _latest(session, src.id)
    new = Prompt(user_id=src.user_id, name=new_name or f"{src.name} 副本", description=src.description)
    session.add(new)
    await session.flush()
    session.add(PromptVersion(prompt_id=new.id, version=1, body=cur.body,
                              variables_json=cur.variables_json))
    await session.commit()
    publish(src.user_id, "prompt", new.id)
    return new.id
```

```python
# backend/app/routers/prompts.py：
#  - extract_vars/_latest 改从 prompt_service import（删本文件内重复定义）
#  - create/update/delete/rollback/duplicate 体 delegate，仍返回 await _detail(...)
from app.services import prompt_service
from app.services.prompt_service import extract_vars, _latest   # 复用，删本文件内定义

@router.post("")
async def create_prompt(body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    pid = await prompt_service.create_prompt(session, user.id, name=body.name,
                                             description=body.description, body=body.body)
    return await _detail(session, user, pid)


@router.put("/{pid}")
async def update_prompt(pid: int, body: PromptIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await prompt_service.update_prompt(session, p, name=body.name, description=body.description, body=body.body)
    return await _detail(session, user, pid)


@router.delete("/{pid}")
async def delete_prompt(pid: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    p = await _get_owned(pid, user, session)
    await prompt_service.delete_prompt(session, p)
    return {"ok": True}


@router.post("/{pid}/rollback")
async def rollback_prompt(pid: int, body: RollbackIn, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_owned(pid, user, session)
    if not await prompt_service.rollback_prompt(session, pid, body.version):
        raise HTTPException(status_code=404, detail="版本不存在")
    return await _detail(session, user, pid)


@router.post("/{pid}/duplicate")
async def duplicate_prompt(pid: int, body: DuplicateIn, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    src = await _get_owned(pid, user, session)
    new_id = await prompt_service.duplicate_prompt(session, src, body.name)
    return await _detail(session, user, new_id)
```

> 注：删 prompts.py 内的 `extract_vars`/`_latest` 定义后，`_detail`/`list_prompts` 等仍调 `_latest`/`extract_vars`——改用 import 来的同名函数即可（签名一致）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_prompt_service.py tests/test_prompts.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/prompt_service.py backend/app/routers/prompts.py backend/tests/test_prompt_service.py
git commit -m "refactor(prompt): 版本化 CRUD 抽 prompt_service 单点 REST+Agent 共用"
```

---

### Task 5: dataset_service 抽取（delete 级联 + ingest_file 上传）+ datasets.py delegate

**Files:**
- Create: `backend/app/services/dataset_service.py`
- Modify: `backend/app/routers/datasets.py`（upload 每文件体 → `ingest_file`；delete → `delete_dataset`）
- Test: `backend/tests/test_dataset_service.py`（新）、既有 `tests/test_datasets.py` 守不变

**Interfaces:**
- Consumes: `ingest_manager`（ingest_manager.py）、`dataset_root`/`detect_upload_structure`（datasets 现有 import）、`settings.data_dir`。
- Produces:
  - `async delete_dataset(session, ds, data_dir) -> None`（删 DatasetRow + 分片目录 + 源文件回收 + commit + publish）
  - `async ingest_file(session, factory, user_id, original_filename, file_path) -> list[Dataset]`（探测结构→建占位行→提交摄入；空 units 返回 []）

> upload 端点的「落盘 UploadFile + 体积/磁盘闸」留在 router（HTTP 语义）；探测+占位+摄入这段（datasets.py:248-277）移入 `ingest_file`。Agent `upload_dataset` 工具复用 `ingest_file`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_dataset_service.py
import json
from pathlib import Path
from app.config import settings
from app.models import Dataset, DatasetRow, User
from app.services import dataset_service
from sqlalchemy import func, select


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="du"); s.add(u); await s.commit(); return u.id


async def test_ingest_file_creates_dataset_from_jsonl(session_factory, tmp_path):
    sf = session_factory; uid = await _seed_user(sf)
    src = tmp_path / "seed.jsonl"
    src.write_text('{"q": "a"}\n{"q": "b"}\n', encoding="utf-8")
    async with sf() as s:
        created = await dataset_service.ingest_file(s, sf, uid, "seed.jsonl", src)
    assert len(created) == 1
    ds_id = created[0].id
    # 摄入后台异步：轮询到 ready
    import asyncio
    for _ in range(40):
        async with sf() as s:
            if (await s.get(Dataset, ds_id)).status == "ready":
                break
        await asyncio.sleep(0.1)
    async with sf() as s:
        assert (await s.get(Dataset, ds_id)).status == "ready"


async def test_delete_dataset_cascades_rows(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        ds = Dataset(user_id=uid, name="d", source="upload", row_count=1,
                     columns_json="[]", status="ready", file_path="")
        s.add(ds); await s.flush()
        s.add(DatasetRow(dataset_id=ds.id, row_idx=0, data_json="{}"))
        await s.commit(); ds_id = ds.id
    async with sf() as s:
        ds = await s.get(Dataset, ds_id)
        await dataset_service.delete_dataset(s, ds, settings.data_dir)
    async with sf() as s:
        assert await s.get(Dataset, ds_id) is None
        cnt = (await s.execute(select(func.count()).select_from(DatasetRow)
               .where(DatasetRow.dataset_id == ds_id))).scalar()
        assert cnt == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_dataset_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 service + router delegate**

```python
# backend/app/services/dataset_service.py
"""数据集写入服务单点：删除级联 + 上传摄入收口，REST 与 Agent DatasetToolkit 共用。"""
import json
import shutil
from pathlib import Path

from sqlalchemy import delete as sa_delete, select

from app.events import publish
from app.models import Dataset, DatasetRow
from app.services import ingest_manager
from app.services.dataset_store import dataset_root
from app.services.upload_parse import detect_upload_structure   # 与 datasets.py 同源（确认实际导入路径）


async def delete_dataset(session, ds: Dataset, data_dir) -> None:
    uid, ds_id, version, file_path = ds.user_id, ds.id, ds.version, ds.file_path
    shard_dir = dataset_root(data_dir, ds.user_id, ds.id, ds.version).parent
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds_id))
    await session.delete(ds)
    await session.commit()
    shutil.rmtree(shard_dir, ignore_errors=True)
    if file_path:
        still = (await session.execute(select(Dataset.id).where(
            Dataset.file_path == str(file_path), Dataset.status == "importing"))).first()
        if still is None:
            Path(file_path).unlink(missing_ok=True)
    publish(uid, "dataset", ds_id)


async def ingest_file(session, factory, user_id: int, original_filename: str,
                      file_path: Path) -> list[Dataset]:
    """探测文件结构→建占位行(importing)→提交后台摄入。空数据(无 unit)返回 []。
    调用方负责文件已落在可读路径（REST=uploads/，Agent=会话工作目录拷进 uploads/）。"""
    units = detect_upload_structure(original_filename, file_path)
    if not units:
        return []
    created = []
    for unit in units:
        ds = Dataset(user_id=user_id, name=unit.name, source="upload",
                     original_filename=original_filename, original_format=unit.original_format,
                     file_path=str(file_path), row_count=0,
                     columns_json=json.dumps(unit.columns, ensure_ascii=False),
                     status="importing", header_row=unit.header_row,
                     data_start_row=unit.data_start_row, total_rows_including_header=0)
        session.add(ds)
        created.append((ds, unit))
    await session.commit()
    out = []
    for ds, unit in created:
        ingest_manager.submit(ds.id, source_path=file_path, unit=unit,
                              version=ds.version, user_id=user_id, session_factory=factory)
        publish(user_id, "dataset", ds.id)
        out.append(ds)
    return out
```

> **实现者注**：`detect_upload_structure` 的真实导入路径以 `datasets.py` 顶部为准（照搬其 import）；`dataset_store.dataset_root` 同理。delete 体逐行对照 datasets.py:434-454 防漂移。

```python
# backend/app/routers/datasets.py：
#  - upload 端点每文件体的「探测+占位+摄入」(248-277) 改调 dataset_service.ingest_file
#  - delete_dataset 体改调 dataset_service.delete_dataset
from app.services import dataset_service   # 顶部加

# upload 内，替换 248-277 段为：
        try:
            created = await dataset_service.ingest_file(session, factory, user.id, original_name, file_path)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"{original_name} parse failed: {exc}") from exc
        if not created:
            file_path.unlink(missing_ok=True)
            continue
        results.extend(_out(ds) for ds in created)

# delete_dataset 体替换为：
@router.delete("/{ds_id}")
async def delete_dataset(ds_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    await dataset_service.delete_dataset(session, ds, settings.data_dir)
    return {"ok": True}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_dataset_service.py tests/test_datasets.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/dataset_service.py backend/app/routers/datasets.py backend/tests/test_dataset_service.py
git commit -m "refactor(dataset): delete 级联 + ingest_file 抽 dataset_service 单点 REST+Agent 共用"
```

---

### Task 6: RunToolkit 读工具

**Files:**
- Create: `backend/app/agent/run_tools.py`（先只放读工具 + 类骨架）
- Test: `backend/tests/test_run_tools.py`

**Interfaces:**
- Consumes: `_fit_budget`（data_preview）、run_service 聚合。
- Produces: `RunToolkit(session_factory, user_id, confirm_delete=False)`，本任务实现读方法 `list_runs`/`get_run`/`read_run_rows`/`read_run_logs`/`read_run_qc`，各返回 JSON 串；他人 run→`{"error":"run_not_found"}`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_run_tools.py
import json
from app.agent.run_tools import RunToolkit
from app.models import (ModelCallLog, QcMetric, Run, RunNodeState, RunRow, User, Workflow,
                        WorkflowVersion)


async def _seed_run(sf):
    async with sf() as s:
        u = User(username="rt"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="W", graph_json='{"nodes":[],"edges":[]}')
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json='{"nodes":[],"edges":[]}')
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.flush()
        s.add(RunRow(run_id=run.id, node_id="o", row_idx=0, status="done", data_json='{"ans":"x"}'))
        s.add(RunNodeState(run_id=run.id, node_id="o", status="done", total=1, done=1, failed=0))
        await s.commit()
        return u.id, wf.id, run.id


async def test_list_runs(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    out = json.loads(await RunToolkit(sf, uid).list_runs())
    assert any(r["id"] == run_id and r["workflow_name"] == "W" for r in out["rows"])


async def test_get_run_and_rows(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    detail = json.loads(await RunToolkit(sf, uid).get_run(run_id))
    assert detail["status"] == "completed"
    rows = json.loads(await RunToolkit(sf, uid).read_run_rows(run_id, "o"))
    assert rows["rows"][0]["data"]["ans"] == "x"


async def test_run_reads_cross_tenant(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    out = json.loads(await RunToolkit(sf, uid + 999).get_run(run_id))
    assert out.get("error") == "run_not_found"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_run_tools.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现读工具 + 类骨架**

```python
# backend/app/agent/run_tools.py
"""Agent run 控制工具：直连 DB + 归属校验的 pydantic-ai 工具，写入复用 run_service 单点。
范式同 GraphToolkit：读返回 JSON 串、错误返回人话串，绝不抛框架。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                        Workflow)


class RunToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned_run(self, s, run_id: int):
        run = await s.get(Run, int(run_id))
        return run if run is not None and run.user_id == self._uid else None

    async def list_runs(self, workflow_id: int | None = None) -> str:
        """列本租户运行(id/工作流名/状态/创建时间/QC首轮通过)。可按 workflow_id 筛。"""
        async with self._sf() as s:
            stmt = (select(Run, Workflow.name).join(Workflow, Run.workflow_id == Workflow.id)
                    .where(Run.user_id == self._uid).order_by(Run.id.desc()))
            if workflow_id is not None:
                stmt = stmt.where(Run.workflow_id == workflow_id)
            rows = (await s.execute(stmt)).all()
            return json.dumps(_fit_budget({"rows": [
                {"id": r.id, "workflow_id": r.workflow_id, "workflow_name": name,
                 "status": r.status, "error": r.error, "created_at": r.created_at.isoformat()}
                for r, name in rows]}), ensure_ascii=False)

    async def get_run(self, run_id: int) -> str:
        """看单次运行状态/统计/各节点进度(total/done/failed)/错误。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            states = (await s.execute(
                select(RunNodeState).where(RunNodeState.run_id == run.id))).scalars().all()
            return json.dumps({
                "id": run.id, "status": run.status, "error": run.error,
                "stats": json.loads(run.stats_json),
                "node_states": [{"node_id": st.node_id, "status": st.status, "total": st.total,
                                 "done": st.done, "failed": st.failed} for st in states]},
                ensure_ascii=False)

    async def read_run_rows(self, run_id: int, node_id: str, status: str | None = None,
                            limit: int = 20) -> str:
        """读运行某节点的输出/失败行。status 可选 done/failed 筛选。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            stmt = select(RunRow).where(RunRow.run_id == run.id, RunRow.node_id == node_id)
            if status is not None:
                stmt = stmt.where(RunRow.status == status)
            rows = (await s.execute(stmt.order_by(RunRow.row_idx)
                    .limit(min(max(int(limit), 1), 100)))).scalars().all()
            return json.dumps(_fit_budget({"rows": [
                {"row_idx": r.row_idx, "status": r.status, "data": json.loads(r.data_json)}
                for r in rows]}), ensure_ascii=False)

    async def read_run_logs(self, run_id: int, kind: str = "system",
                            node_id: str | None = None, limit: int = 100) -> str:
        """读运行日志：kind=system 系统日志 / kind=model 模型调用日志(可按 node_id 筛)。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            cap = min(max(int(limit), 1), 200)
            if kind == "model":
                stmt = select(ModelCallLog).where(ModelCallLog.run_id == run.id)
                if node_id is not None:
                    stmt = stmt.where(ModelCallLog.node_id == node_id)
                ms = (await s.execute(stmt.order_by(ModelCallLog.id.desc()).limit(cap))).scalars().all()
                data = [{"node_id": m.node_id, "source": m.source, "model_name": m.model_name,
                         "completion_tokens": m.completion_tokens} for m in ms]
            else:
                ls = (await s.execute(select(RunLog).where(RunLog.run_id == run.id)
                      .order_by(RunLog.id).limit(cap))).scalars().all()
                data = [{"node_id": l.node_id, "level": l.level, "message": l.message} for l in ls]
            return json.dumps(_fit_budget({"rows": data}), ensure_ascii=False)

    async def read_run_qc(self, run_id: int, node_id: str | None = None, limit: int = 20) -> str:
        """读运行质检：各 QC 节点指标(总数/首轮通过) + 失败样本(含各模型理由)。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            metrics = (await s.execute(
                select(QcMetric).where(QcMetric.run_id == run.id))).scalars().all()
            fstmt = select(QcFailure).where(QcFailure.run_id == run.id)
            if node_id is not None:
                fstmt = fstmt.where(QcFailure.node_id == node_id)
            fails = (await s.execute(fstmt.order_by(QcFailure.id)
                     .limit(min(max(int(limit), 1), 100)))).scalars().all()
            return json.dumps(_fit_budget({
                "metrics": [{"node_id": m.node_id, "total": m.total,
                             "first_round_pass": m.first_round_pass} for m in metrics],
                "failures": [{"node_id": f.node_id, "sample": json.loads(f.sample_json),
                              "reasons": json.loads(f.reasons_json)} for f in fails]},
                key="failures"), ensure_ascii=False)

    @property
    def tools(self) -> list:
        return [self.list_runs, self.get_run, self.read_run_rows,
                self.read_run_logs, self.read_run_qc]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_run_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/run_tools.py backend/tests/test_run_tools.py
git commit -m "feat(agent): RunToolkit 读工具(list_runs/get_run/rows/logs/qc)"
```

---

### Task 7: RunToolkit 写工具（start/cancel/rerun + restore/delete/delete_all 门禁）

**Files:**
- Modify: `backend/app/agent/run_tools.py`（加写方法 + 扩 `.tools`）
- Test: `backend/tests/test_run_tools.py`（扩）

**Interfaces:**
- Consumes: `enqueue_run`/`validate_graph_resource_ownership`/`purge_run_rows`/`unlink_run_exports`/`restore_workflow_from_run`（run_service）、`manager`（engine.manager）、`parse_graph`/`validate_graph`（engine.graph）、`settings.data_dir`。
- Produces 写方法：`start_run`/`cancel_run`/`rerun_failed`/`restore_workflow_from_run`🔒/`delete_run`🔒/`delete_all_runs`🔒。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_run_tools.py 追加
from app.services import run_service


async def test_delete_run_requires_confirmation(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid).delete_run(run_id)               # 默认未确认
    assert "确认" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is not None                 # 未删


async def test_delete_run_confirmed_cascades(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid, confirm_delete=True).delete_run(run_id)
    assert "已删除" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is None
        cnt = (await s.execute(select(func.count()).select_from(RunRow)
               .where(RunRow.run_id == run_id))).scalar()
        assert cnt == 0


async def test_restore_workflow_requires_confirmation(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid).restore_workflow_from_run(run_id)
    assert "确认" in msg


async def test_delete_all_runs_confirmed(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid, confirm_delete=True).delete_all_runs()
    assert "删除" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is None
```

（`from sqlalchemy import func, select` 顶部已在 test 文件，需补 `func`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_run_tools.py -q`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现写工具**

```python
# backend/app/agent/run_tools.py 顶部加 import（并把 Task6 的 models import 补上 User）
from app.config import settings
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.engine.manager import manager
from app.models import User   # 补进 Task6 已有的 models import 行
from app.routers.runs import _prepare_rerun_failed   # 复用 REST rerun 的 prepare 单点
from app.services.run_service import (enqueue_run, purge_run_rows,
                                      restore_workflow_from_run as _restore_from_run,
                                      unlink_run_exports, validate_graph_resource_ownership)

# 在 RunToolkit 内加方法（@property tools 末尾追加这些）：

    async def start_run(self, workflow_id: int) -> str:
        """启动一次运行(不阻塞，返回 run_id；用 get_run 看进度)。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await s.get(Workflow, int(workflow_id))
            if wf is None or wf.user_id != self._uid:
                return "工作流不存在"
            try:
                graph = parse_graph(wf.graph_json)
                validate_graph(graph)
                if not graph.nodes:
                    raise GraphError("工作流为空")
                await validate_graph_resource_ownership(s, graph, self._uid)
            except (GraphError, ValueError) as e:
                return f"Error: {e}"
        run_id = await enqueue_run(self._sf, self._uid, workflow_id)
        return f"已启动运行 #{run_id}（排队中），用 get_run({run_id}) 看进度"

    async def cancel_run(self, run_id: int) -> str:
        """取消运行中/排队中的运行。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return "运行不存在"
            if run.status not in ("queued", "running"):
                return f"Error: 当前状态 {run.status} 不可取消"
        manager.cancel(int(run_id))
        return f"已请求取消运行 #{run_id}"

    async def rerun_failed(self, run_id: int, node_id: str | None = None) -> str:
        """重跑失败行(可指定节点)。复用 manager 入队。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return "运行不存在"
            cap = (await s.get(User, self._uid)).max_llm_concurrency
        await _prepare_rerun_failed(self._sf, int(run_id), node_id, self._uid)
        manager.submit(int(run_id), self._uid, cap, self._sf)
        return f"已重跑运行 #{run_id} 的失败行"

    async def restore_workflow_from_run(self, run_id: int) -> str:
        """把工作流图恢复到该运行的版本(覆盖当前图)。需用户确认。
        Parameters:
            run_id: 运行 id
        """
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return "运行不存在"
            if not self._confirm_delete:
                return ("恢复工作流版本会覆盖当前图(丢失当前未跑的编辑)，需用户确认："
                        f"请说明后在回复末尾单独一行输出 [confirm_delete] 恢复运行#{run_id}的版本，等待确认。")
            wf = await _restore_from_run(s, run, self._uid)   # 别名调 service，避免与本方法同名遮蔽
            return f"已把工作流恢复到运行 #{run_id} 的版本" if wf else "工作流不存在"

    async def delete_run(self, run_id: int) -> str:
        """删除单次运行(级联子表+磁盘导出)。需用户确认。
        Parameters:
            run_id: 运行 id
        """
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return "运行不存在"
            if run.status in ("queued", "running"):
                return "Error: 运行中，请先取消再删除"
            if not self._confirm_delete:
                return ("删除运行需用户确认：请向用户说明将删除运行及其全部行/日志/质检/导出，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf rmrun {run_id}，然后结束回合等待确认。")
            ver_id = run.workflow_version_id
            await purge_run_rows(s, [int(run_id)], version_ids=[ver_id])
            await s.commit()
        unlink_run_exports([int(run_id)], settings.data_dir)
        return f"已删除运行 #{run_id}"

    async def delete_all_runs(self) -> str:
        """清空本租户全部运行(运行中除外)。需用户确认。"""
        async with self._sf() as s:
            runs = (await s.execute(select(Run).where(
                Run.user_id == self._uid, Run.status.notin_(("queued", "running"))))).scalars().all()
            if not self._confirm_delete:
                return ("清空全部运行需用户确认：请向用户说明将删除全部已结束运行及其数据，"
                        "在回复末尾单独一行输出 [confirm_delete] 清空全部运行记录，然后结束回合等待确认。")
            run_ids = [r.id for r in runs]
            ver_ids = [r.workflow_version_id for r in runs]
            if run_ids:
                await purge_run_rows(s, run_ids, version_ids=ver_ids)
                await s.commit()
        if run_ids:
            unlink_run_exports(run_ids, settings.data_dir)
        return f"已删除 {len(run_ids)} 条运行记录"
```

> **实现者注**：`rerun_failed` 里取 capacity 的写法改用顶部 `from app.models import User`，避免 `__import__` 丑写法——见 file 顶部已 import 的 models 列表，补 `User`。`_prepare_rerun_failed` 是 routers.runs 现有 helper（runs.py:278 调用处），从 routers.runs 直接 import 复用。
> 扩 `.tools`：`return [...读5个..., self.start_run, self.cancel_run, self.rerun_failed, self.restore_workflow_from_run, self.delete_run, self.delete_all_runs]`

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_run_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/run_tools.py backend/tests/test_run_tools.py
git commit -m "feat(agent): RunToolkit 写工具(start/cancel/rerun + restore/delete/delete_all 门禁)"
```

---

### Task 8: ModelToolkit（create/update/delete🔒/test）

**Files:**
- Create: `backend/app/agent/model_tools.py`
- Test: `backend/tests/test_model_tools.py`

**Interfaces:**
- Consumes: `model_service`（Task 3）、`crypto.decrypt`（断言用）、`llm.chat`（test）。
- Produces: `ModelToolkit(session_factory, user_id, confirm_delete=False)`，工具 `create_model`/`update_model`/`delete_model`🔒/`test_model`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_model_tools.py
from app import crypto
from app.agent.model_tools import ModelToolkit
from app.models import ModelConfig, User


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="mt"); s.add(u); await s.commit(); return u.id


async def test_create_model_tool(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    msg = await ModelToolkit(sf, uid).create_model(
        name="m1", base_url="http://x", model_name="gpt", api_key="sk-z")
    async with sf() as s:
        mc = (await s.execute(__import__("sqlalchemy").select(ModelConfig))).scalars().first()
        assert mc.name == "m1" and crypto.decrypt(mc.api_key_enc) == "sk-z"
    assert "已创建" in msg and "sk-z" not in msg   # 返回串不回显密钥


async def test_delete_model_requires_confirmation(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        mc = ModelConfig(user_id=uid, name="m", model_name="g", base_url="u",
                         api_key_enc="", default_params_json="{}"); s.add(mc); await s.commit(); mid = mc.id
    msg = await ModelToolkit(sf, uid).delete_model(mid)
    assert "确认" in msg
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is not None
    msg2 = await ModelToolkit(sf, uid, confirm_delete=True).delete_model(mid)
    assert "已删除" in msg2
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is None


async def test_model_tool_cross_tenant(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        mc = ModelConfig(user_id=uid, name="m", model_name="g", base_url="u",
                         api_key_enc="", default_params_json="{}"); s.add(mc); await s.commit(); mid = mc.id
    assert "不存在" in await ModelToolkit(sf, uid + 999).delete_model(mid)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_model_tools.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 ModelToolkit**

```python
# backend/app/agent/model_tools.py
"""Agent 模型配置写工具：复用 model_service 单点；api_key 为顶层参数(经 _brief 打码)。"""
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import ModelConfig
from app.services import llm, model_service


class ModelToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned(self, s, mc_id: int):
        mc = await s.get(ModelConfig, int(mc_id))
        return mc if mc is not None and mc.user_id == self._uid else None

    async def create_model(self, name: str, base_url: str, model_name: str,
                           api_key: str | None = None, provider: str = "openai",
                           api_version: str = "") -> str:
        """新建模型配置。api_key 会加密存储、不回显。
        Parameters:
            name: 配置名
            base_url: API 基址
            model_name: 模型 ID(如 deepseek-chat)
            api_key: 密钥(可选；留空可后续补)
            provider: openai 或 azure
            api_version: azure legacy 需填
        """
        async with self._sf() as s:
            mc = await model_service.create_model(
                s, self._uid, name=name, model_name=model_name, base_url=base_url,
                provider=provider, azure_api_mode="legacy", api_version=api_version,
                api_key=api_key or "")
            return f"已创建模型「{name}」(#{mc.id})"

    async def update_model(self, model_id: int, name: str | None = None, base_url: str | None = None,
                           model_name: str | None = None, api_key: str | None = None) -> str:
        """修改模型配置(只改给出的字段；api_key 留空=不改)。
        Parameters:
            model_id: 模型配置 id
        """
        async with self._sf() as s:
            mc = await self._owned(s, model_id)
            if mc is None:
                return "模型配置不存在"
            await model_service.update_model(
                s, mc, name=name if name is not None else mc.name,
                model_name=model_name if model_name is not None else mc.model_name,
                base_url=base_url if base_url is not None else mc.base_url,
                provider=mc.provider, azure_api_mode=mc.azure_api_mode, api_version=mc.api_version,
                default_params=json.loads(mc.default_params_json),
                api_key=api_key or "")
            return f"已更新模型 #{model_id}"

    async def delete_model(self, model_id: int) -> str:
        """删除模型配置。需用户确认。
        Parameters:
            model_id: 模型配置 id
        """
        async with self._sf() as s:
            mc = await self._owned(s, model_id)
            if mc is None:
                return "模型配置不存在"
            if not self._confirm_delete:
                return ("删除模型配置需用户确认：请向用户说明，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf model rm {model_id}，然后结束回合等待确认。")
            await model_service.delete_model(s, mc)
            return f"已删除模型配置 #{model_id}"

    async def test_model(self, model_id: int) -> str:
        """连通测试：真实发一条请求(会产生少量费用)。
        Parameters:
            model_id: 模型配置 id
        """
        async with self._sf() as s:
            mc = await self._owned(s, model_id)
            if mc is None:
                return "模型配置不存在"
            try:
                text, _ = await llm.chat(mc, "", "这是一次连通测试，没问题回复OK",
                                         params={"max_tokens": 65536}, retries=1)
                return f"连通正常：{text[:100]}"
            except llm.LLMError as e:
                return f"连通失败：{e}"

    @property
    def tools(self) -> list:
        return [self.create_model, self.update_model, self.delete_model, self.test_model]
```

> **实现者注**：把 `__import__("json")` 改为文件顶部 `import json`（测试里的 `__import__` 同理仅为示意，实现请正常 import）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_model_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/model_tools.py backend/tests/test_model_tools.py
git commit -m "feat(agent): ModelToolkit(create/update/delete门禁/test，密钥顶层参数)"
```

---

### Task 9: DatasetToolkit（upload/export/delete🔒）

**Files:**
- Create: `backend/app/agent/dataset_tools.py`
- Test: `backend/tests/test_dataset_tools.py`

**Interfaces:**
- Consumes: `dataset_service.ingest_file`/`delete_dataset`（Task 5）、`resolve_in`（沙箱）、`settings.data_dir`、导出复用 `app.services.dataset_export`（确认实际导出函数名，与 datasets.py export 同源）。
- Produces: `DatasetToolkit(session_factory, user_id, workdir, confirm_delete=False)`，工具 `upload_dataset`/`export_dataset`/`delete_dataset`🔒。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_dataset_tools.py
import asyncio
import json
from app.agent.dataset_tools import DatasetToolkit
from app.models import Dataset, User


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="dt"); s.add(u); await s.commit(); return u.id


async def test_upload_dataset_from_workdir(session_factory, tmp_path):
    sf = session_factory; uid = await _seed_user(sf)
    (tmp_path / "seed.jsonl").write_text('{"q":"a"}\n{"q":"b"}\n', encoding="utf-8")
    msg = await DatasetToolkit(sf, uid, tmp_path).upload_dataset("seed.jsonl")
    assert "上传" in msg or "摄入" in msg
    async with sf() as s:
        ds = (await s.execute(__import__("sqlalchemy").select(Dataset))).scalars().first()
        assert ds is not None


async def test_upload_dataset_path_escape_blocked(session_factory, tmp_path):
    sf = session_factory; uid = await _seed_user(sf)
    msg = await DatasetToolkit(sf, uid, tmp_path).upload_dataset("../../etc/passwd")
    assert "Security error" in msg or "Error" in msg


async def test_delete_dataset_requires_confirmation(session_factory, tmp_path):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        ds = Dataset(user_id=uid, name="d", source="upload", row_count=0,
                     columns_json="[]", status="ready", file_path=""); s.add(ds); await s.commit(); did = ds.id
    msg = await DatasetToolkit(sf, uid, tmp_path).delete_dataset(did)
    assert "确认" in msg
    async with sf() as s:
        assert await s.get(Dataset, did) is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_dataset_tools.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 DatasetToolkit**

```python
# backend/app/agent/dataset_tools.py
"""Agent 数据集写工具：上传/导出走会话工作目录沙箱(resolve_in)，复用 dataset_service 单点。"""
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db import get_session_factory
from app.models import Dataset
from app.services import dataset_service
from app.agent.tools import resolve_in   # 沙箱解析(越界抛 ValueError)


class DatasetToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int, workdir,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._workdir = Path(workdir)
        self._confirm_delete = confirm_delete

    async def _owned(self, s, ds_id: int):
        ds = await s.get(Dataset, int(ds_id))
        return ds if ds is not None and ds.user_id == self._uid else None

    async def upload_dataset(self, file_path: str, name: str | None = None) -> str:
        """把会话工作目录里的文件(jsonl/json/csv/xlsx/xls)上传为数据集(异步摄入)。
        Parameters:
            file_path: 文件路径，相对会话工作目录(先用 write_file 落文件)
            name: 可选数据集名(留空用文件名)
        """
        try:
            src = resolve_in(self._workdir, file_path)
        except ValueError as e:
            return f"Security error: {e}"
        if not src.exists():
            return f"Error: 文件不存在 {file_path}"
        # 拷进 uploads/ 作摄入源(摄入会回收源文件，勿直接吃掉用户工作目录文件)
        upload_dir = settings.data_dir / "uploads" / str(self._uid)
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{uuid4().hex[:8]}_{src.name}"
        shutil.copy2(src, dest)
        original = name or src.name
        async with self._sf() as s:
            try:
                created = await dataset_service.ingest_file(s, get_session_factory(), self._uid,
                                                            original, dest)
            except (ValueError, UnicodeDecodeError, RecursionError) as e:
                dest.unlink(missing_ok=True)
                return f"Error: 解析失败 {e}"
        if not created:
            dest.unlink(missing_ok=True)
            return "Error: 文件无可用数据"
        return "已上传摄入(后台进行中)：" + ", ".join(f"{d.name}(#{d.id})" for d in created)

    async def export_dataset(self, dataset_id: int, format: str = "jsonl") -> str:
        """把数据集导出到会话工作目录。format=jsonl/csv/xlsx。
        Parameters:
            dataset_id: 数据集 id
            format: jsonl/csv/xlsx
        """
        async with self._sf() as s:
            ds = await self._owned(s, dataset_id)
            if ds is None:
                return "数据集不存在"
        out_rel = f"dataset_{dataset_id}.{format}"
        out_path = resolve_in(self._workdir, out_rel)
        # 复用 datasets.py export 的写盘逻辑(与导出端点同源函数)
        from app.services.dataset_export import write_dataset_export
        await write_dataset_export(self._sf, dataset_id, format, out_path)
        return f"已导出到工作目录 {out_rel}"

    async def delete_dataset(self, dataset_id: int) -> str:
        """删除数据集(级联删行/磁盘分片/源文件)。需用户确认。
        Parameters:
            dataset_id: 数据集 id
        """
        async with self._sf() as s:
            ds = await self._owned(s, dataset_id)
            if ds is None:
                return "数据集不存在"
            if not self._confirm_delete:
                return ("删除数据集需用户确认：请向用户说明将删除数据集及其全部行与磁盘文件，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf data rm {dataset_id}，然后结束回合等待确认。")
            await dataset_service.delete_dataset(s, ds, settings.data_dir)
            return f"已删除数据集 #{dataset_id}"

    @property
    def tools(self) -> list:
        return [self.upload_dataset, self.export_dataset, self.delete_dataset]
```

> **实现者注**：`write_dataset_export` 的真实函数名/签名以 `datasets.py` export 端点(359-400)实际调用为准；若导出逻辑内联在端点未抽函数，则本任务**附带**把它抽成 `dataset_export.write_dataset_export(session_factory, dataset_id, format, out_path)` 供端点与本工具共用（同 Task 3-5 抽取范式）。`export_dataset` 测试到 ready 数据集导出非空文件即可（happy path 留活体）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_dataset_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/dataset_tools.py backend/tests/test_dataset_tools.py
git commit -m "feat(agent): DatasetToolkit(upload/export/delete门禁，工作目录沙箱)"
```

---

### Task 10: PromptToolkit（create/update/delete🔒/list_versions/rollback/duplicate）

**Files:**
- Create: `backend/app/agent/prompt_tools.py`
- Test: `backend/tests/test_prompt_tools.py`

**Interfaces:**
- Consumes: `prompt_service`（Task 4）。
- Produces: `PromptToolkit(session_factory, user_id, confirm_delete=False)`，工具 `create_prompt`/`update_prompt`/`delete_prompt`🔒/`list_prompt_versions`/`rollback_prompt`/`duplicate_prompt`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_prompt_tools.py
import json
from app.agent.prompt_tools import PromptToolkit
from app.models import Prompt, PromptVersion, User
from sqlalchemy import func, select


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="pt"); s.add(u); await s.commit(); return u.id


async def test_create_and_list_versions(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    msg = await PromptToolkit(sf, uid).create_prompt(name="p", body="你好 {{q}}")
    import re
    pid = int(re.search(r"#(\d+)", msg).group(1))
    vers = json.loads(await PromptToolkit(sf, uid).list_prompt_versions(pid))
    assert vers["rows"][0]["version"] == 1


async def test_delete_prompt_requires_confirmation(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        p = Prompt(user_id=uid, name="p", description=""); s.add(p); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="x", variables_json="[]"))
        await s.commit(); pid = p.id
    assert "确认" in await PromptToolkit(sf, uid).delete_prompt(pid)
    async with sf() as s:
        assert await s.get(Prompt, pid) is not None
    assert "已删除" in await PromptToolkit(sf, uid, confirm_delete=True).delete_prompt(pid)


async def test_rollback_tool(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        p = Prompt(user_id=uid, name="p", description=""); s.add(p); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="老", variables_json="[]"))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="新", variables_json="[]"))
        await s.commit(); pid = p.id
    assert "已回滚" in await PromptToolkit(sf, uid).rollback_prompt(pid, 1)
    async with sf() as s:
        cnt = (await s.execute(select(func.count()).select_from(PromptVersion)
               .where(PromptVersion.prompt_id == pid))).scalar()
        assert cnt == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_prompt_tools.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 PromptToolkit**

```python
# backend/app/agent/prompt_tools.py
"""Agent 提示词库写工具：复用 prompt_service 单点(版本化语义)。list/get 复用 catalog，不重复。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.models import Prompt, PromptVersion
from app.services import prompt_service


class PromptToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned(self, s, pid: int):
        p = await s.get(Prompt, int(pid))
        return p if p is not None and p.user_id == self._uid else None

    async def create_prompt(self, name: str, body: str, description: str = "") -> str:
        """新建库提示词(自动建 v1、提取 {{变量}})。
        Parameters:
            name: 提示词名
            body: 正文(可含 {{变量}} 占位符)
            description: 描述
        """
        async with self._sf() as s:
            pid = await prompt_service.create_prompt(s, self._uid, name=name,
                                                     description=description, body=body)
            return f"已创建提示词「{name}」(#{pid})"

    async def update_prompt(self, prompt_id: int, body: str | None = None,
                            name: str | None = None, description: str | None = None) -> str:
        """改库提示词(仅正文变更才出新版本；名/描述原地改)。
        Parameters:
            prompt_id: 提示词 id
        """
        async with self._sf() as s:
            p = await self._owned(s, prompt_id)
            if p is None:
                return "提示词不存在"
            cur = await prompt_service._latest(s, p.id)
            await prompt_service.update_prompt(
                s, p, name=name if name is not None else p.name,
                description=description if description is not None else p.description,
                body=body if body is not None else cur.body)
            return f"已更新提示词 #{prompt_id}"

    async def delete_prompt(self, prompt_id: int) -> str:
        """删除库提示词(级联删全部版本)。需用户确认。
        Parameters:
            prompt_id: 提示词 id
        """
        async with self._sf() as s:
            p = await self._owned(s, prompt_id)
            if p is None:
                return "提示词不存在"
            if not self._confirm_delete:
                return ("删除提示词需用户确认：请向用户说明将删除提示词及其全部版本，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf prompt rm {prompt_id}，然后结束回合等待确认。")
            await prompt_service.delete_prompt(s, p)
            return f"已删除提示词 #{prompt_id}"

    async def list_prompt_versions(self, prompt_id: int) -> str:
        """列某提示词的全部版本(版本号/正文摘要)。
        Parameters:
            prompt_id: 提示词 id
        """
        async with self._sf() as s:
            p = await self._owned(s, prompt_id)
            if p is None:
                return json.dumps({"error": "prompt_not_found"}, ensure_ascii=False)
            vers = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id)
                    .order_by(PromptVersion.version))).scalars().all()
            return json.dumps(_fit_budget({"rows": [
                {"version": v.version, "body": v.body[:200]} for v in vers]}), ensure_ascii=False)

    async def rollback_prompt(self, prompt_id: int, version: int) -> str:
        """把提示词回滚到历史版本(复制其正文成新版)。
        Parameters:
            prompt_id: 提示词 id
            version: 目标版本号
        """
        async with self._sf() as s:
            p = await self._owned(s, prompt_id)
            if p is None:
                return "提示词不存在"
            ok = await prompt_service.rollback_prompt(s, p.id, int(version))
            return f"已回滚提示词 #{prompt_id} 到版本 {version}" if ok else f"Error: 版本 {version} 不存在"

    async def duplicate_prompt(self, prompt_id: int, name: str | None = None) -> str:
        """复制库提示词为新提示词。
        Parameters:
            prompt_id: 源提示词 id
            name: 新名(留空=原名+副本)
        """
        async with self._sf() as s:
            src = await self._owned(s, prompt_id)
            if src is None:
                return "提示词不存在"
            new_id = await prompt_service.duplicate_prompt(s, src, name)
            return f"已复制为新提示词 #{new_id}"

    @property
    def tools(self) -> list:
        return [self.create_prompt, self.update_prompt, self.delete_prompt,
                self.list_prompt_versions, self.rollback_prompt, self.duplicate_prompt]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_prompt_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/prompt_tools.py backend/tests/test_prompt_tools.py
git commit -m "feat(agent): PromptToolkit(create/update/delete门禁/versions/rollback/dup)"
```

---

### Task 11: GraphToolkit 加 export_workflow/import_workflow

**Files:**
- Modify: `backend/app/agent/graph_tools.py`（构造加 `workdir`；加两工具 + 扩 `.tools`）
- Test: `backend/tests/test_graph_tools.py`（扩）

**Interfaces:**
- Consumes: `export_package`/`import_package`/`PackageError`（workflow_package）、`resolve_in`、`GraphError`。
- Produces: `GraphToolkit(session_factory, user_id, confirm_delete=False, workdir=None)` 新增可选 `workdir`；工具 `export_workflow(workflow_id)`、`import_workflow(file_path)`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_graph_tools.py 追加
async def test_export_then_import_workflow_roundtrip(session_factory, tmp_path):
    sf = session_factory
    g = {"nodes": [{"id": "in", "type": "input", "config": {}}], "edges": []}
    uid, wf_id = await _seed(sf, g)
    tk = GraphToolkit(sf, uid, workdir=tmp_path)
    msg = await tk.export_workflow(wf_id)
    assert ".gfpkg" in msg
    # 导出文件落在工作目录
    pkgs = list(tmp_path.glob("*.gfpkg"))
    assert pkgs
    imp = await tk.import_workflow(pkgs[0].name)
    assert "已导入" in imp


async def test_import_workflow_bad_file_returns_error(session_factory, tmp_path):
    sf = session_factory
    uid, wf_id = await _seed(sf)
    (tmp_path / "bad.gfpkg").write_text("not a zip", encoding="utf-8")
    msg = await GraphToolkit(sf, uid, workdir=tmp_path).import_workflow("bad.gfpkg")
    assert msg.startswith("Error")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -k workflow_roundtrip -q`
Expected: FAIL（构造不收 workdir / 方法不存在）

- [ ] **Step 3: 实现**

```python
# backend/app/agent/graph_tools.py
# 1) 构造加 workdir：
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False, workdir=None):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete
        self._workdir = workdir

# 2) 顶部 import：
from pathlib import Path
from app.agent.tools import resolve_in
from app.services.workflow_package import export_package, import_package, PackageError

# 3) 加两工具：
    async def export_workflow(self, workflow_id: int) -> str:
        """把工作流打包成 .gfpkg 导出到会话工作目录(自包含图+数据集+提示词，模型去密钥)。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            out_rel = f"workflow_{workflow_id}.gfpkg"
            out_path = resolve_in(Path(self._workdir), out_rel)
            await export_package(s, wf, str(out_path))
            return f"已导出 {out_rel}"

    async def import_workflow(self, file_path: str) -> str:
        """从会话工作目录的 .gfpkg 导入一条工作流(同名资源复用、模型需补密钥)。
        Parameters:
            file_path: .gfpkg 路径，相对会话工作目录
        """
        try:
            src = resolve_in(Path(self._workdir), file_path)
        except ValueError as e:
            return f"Security error: {e}"
        if not src.exists():
            return f"Error: 文件不存在 {file_path}"
        async with self._sf() as s:
            try:
                wf_out, report = await import_package(s, str(src), self._uid)
            except (PackageError, GraphError) as e:
                return f"Error: {e}"
        return f"已导入工作流「{wf_out['name']}」(#{wf_out['id']})"

# 4) 扩 .tools，把 export_workflow, import_workflow 追加进列表
```

> **实现者注**：`resolve_in` 越界抛 ValueError，import_workflow 已 catch；export_workflow 路径由我们构造（固定名）不会越界。`workdir` 为 None 时这两工具不会被装配（见 Task 12 装配条件），故方法体可假定 workdir 非 None。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_tools.py -q`
Expected: PASS（含既有 GraphToolkit 全部用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/graph_tools.py backend/tests/test_graph_tools.py
git commit -m "feat(agent): GraphToolkit 加 export_workflow/import_workflow(.gfpkg 工作目录)"
```

---

### Task 12: system.py 装配全部新 toolkit + 透传 confirm_delete/workdir

**Files:**
- Modify: `backend/app/agent/system.py:44-55`（`_make_tools`）
- Test: `backend/tests/test_agent_system.py`（扩）

**Interfaces:**
- Consumes: 全部新 toolkit。
- Produces: 主 Agent 工具集含全部 Phase2 工具；无重名冲突（pydantic-ai agent 能构建）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_agent_system.py 追加
def test_make_tools_includes_phase2_tools(tmp_path):
    echo = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")]))
    sysm = AgentSystem(models={"coordinator": echo, "manager": echo, "worker": echo},
                       workdir=tmp_path, confirm_delete=False, emit=None,
                       user_id=1, session_factory=lambda: None)
    names = {getattr(t, "__name__", "") for t in sysm._make_tools(tmp_path / "s.json")}
    assert {"start_run", "get_run", "create_model", "delete_model", "upload_dataset",
            "create_prompt", "rollback_prompt", "export_workflow", "import_workflow"} <= names


def test_make_tools_no_duplicate_names(tmp_path):
    echo = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")]))
    sysm = AgentSystem(models={"coordinator": echo, "manager": echo, "worker": echo},
                       workdir=tmp_path, confirm_delete=False, emit=None,
                       user_id=1, session_factory=lambda: None)
    names = [getattr(t, "__name__", "") for t in sysm._make_tools(tmp_path / "s.json")]
    assert len(names) == len(set(names)), f"重名: {[n for n in names if names.count(n) > 1]}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_system.py -k phase2 -q`
Expected: FAIL（工具未装配）

- [ ] **Step 3: 实现装配**

```python
# backend/app/agent/system.py 替换 _make_tools 的条件块
        if self._session_factory is not None and self._user_id is not None:
            from app.agent.catalog import make_catalog_tools
            from app.agent.dataset_tools import DatasetToolkit
            from app.agent.graph_tools import GraphToolkit
            from app.agent.model_tools import ModelToolkit
            from app.agent.prompt_tools import PromptToolkit
            from app.agent.run_tools import RunToolkit
            sf, uid, cd = self._session_factory, self._user_id, self._confirm_delete
            tools += GraphToolkit(sf, uid, cd, workdir=self.workdir).tools
            tools += make_catalog_tools(sf, uid)
            tools += RunToolkit(sf, uid, cd).tools
            tools += ModelToolkit(sf, uid, cd).tools
            tools += DatasetToolkit(sf, uid, self.workdir, cd).tools
            tools += PromptToolkit(sf, uid, cd).tools
        return tools
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_agent_system.py -q`
Expected: PASS（含既有装配用例 + run_turn 能构建 agent 无重名冲突）

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd backend && python -m pytest -q`
Expected: PASS（既有 + 新增；`test_run_trace` 偶发 flaky 为既有异步摄入 race，孤立重跑绿）

```bash
git add backend/app/agent/system.py backend/tests/test_agent_system.py
git commit -m "feat(agent): 主 Agent 装配 Run/Model/Dataset/Prompt toolkit + 导入导出，透传 confirm_delete/workdir"
```

---

### Task 13: 活体验证脚本（重启后人工跑）

**Files:**
- Create: `backend/tools/agent_phase2_live.py`

**Interfaces:**
- 对真实后端 :8000 + 真实 DeepSeek（admin#4 复用 deepseek-flash#2），建 `_p2live_` 标签资源，建即删回基线。

- [ ] **Step 1: 写活体脚本**

覆盖（直驱 toolkit，确定性 + 门禁两态 + 一条真跑闭环）：
1. `GraphToolkit(sf, uid, confirm_delete=True, workdir=wd)` 搭 input→llm→output（复用 P1 工具），`RunToolkit.start_run` → 轮询 `get_run` 到 completed → `read_run_rows` 验产出。
2. `PromptToolkit`：create→update(正文变出版)→list_prompt_versions→rollback→delete(先验未确认拦、再 confirm_delete=True 删)。
3. `ModelToolkit`：create_model(带 key)→`_brief` 不含明文断言→test_model 连通→delete(门禁两态)。
4. `DatasetToolkit`：write_file 落 jsonl→upload_dataset→轮询 ready→export_dataset→delete(门禁两态)。
5. `RunToolkit.restore_workflow_from_run`(门禁两态)、`GraphToolkit.export_workflow`→`import_workflow` 往返。
6. Part B：真实主 Agent（真实模型）下指令「建链路→跑→把结果导出→建个提示词」，验证它能自主调 Phase2 工具。
7. cleanup：删所有 `_p2live_` 标签工作流/数据集/模型/提示词 + 会话，回基线断言。

参照 `backend/tools/agent_node_tools_live.py` 结构（Rest 类登录 + 轮询 + 建即删）。

- [ ] **Step 2: 标注为人工步骤**

脚本不进 pytest；交付说明：「合并并重启后端后，`cd backend && PYTHONIOENCODING=utf-8 python tools/agent_phase2_live.py`，期望全 PASS + 回基线」。

- [ ] **Step 3: 提交**

```bash
git add backend/tools/agent_phase2_live.py
git commit -m "test(agent): Phase2 全生命周期工具真实活体脚本(直驱+门禁两态+真Agent闭环)"
```

---

## 完成标准

- 全部 13 任务提交；`cd backend && python -m pytest -q` 全绿（除既有 `test_run_trace` flaky）。
- REST 回归（model/prompt/dataset/runs）行为不变。
- 重启后 `agent_phase2_live.py` 人工跑全 PASS、基线零损失。
- 合并 master（本地不推 origin）；线上需重启生效。
