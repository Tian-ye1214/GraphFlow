# 批20 实现计划：节点助手独立化 + 思考 xhigh 硬编码 + 模型日志 + 折叠布局

> **For agentic workers:** REQUIRED SUB-SKILL：用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐个实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让每个节点拥有独立绑定、可多轮对话的「节点助手」（前端内存隔离会话+草稿+并发），把整个 RedLotus+节点助手的思考硬编码为 xhigh，在两条模型/Agent 网关出口统一记录对话日志（多处可看），并把节点配置面板重做为全分组默认全折叠。

**Architecture:** 思考强制经 `thinking.force_xhigh` 在 `create_model`（覆盖全 agent）+ `compactor` 两点注入；日志走单一切口 `model_log.log_model_call`，上下文用 contextvar 传递，节点路径在 `llm.chat`、Agent 路径包一层 `LoggingModel`(WrapperModel)；节点助手后端改无状态多轮、前端用 `useSyncExternalStore` 模块级 store 按 `workflowId:nodeId` 隔离会话与草稿。

**Tech Stack:** FastAPI + SQLAlchemy2 async(SQLite WAL，无迁移=create_all) + pydantic-ai 1.107 + React19 + AntD6 + Vite + loguru。

## Global Constraints（每个任务都隐含遵守）

- KISS：最简实现，不预先防御未发生的 bug；只修实际触发的问题。
- **铁律：任何日志/响应/错误都绝不输出 api_key、Authorization 头或任何凭据。** 日志只记 messages 与响应文本。
- 每用户隔离（`resource.user_id == user.id`）是硬红线，新查询端点必须校验。
- 无迁移：新表加进 `backend/app/models.py` 即随 `Base.metadata.create_all` 自动建；删表/级联全部手动。
- **commit 不出现 "claude"，不加 Co-Authored-By 尾注**；用中文 `type(scope): 描述`，单条或多条 `-m` 均可。
- 中文路径 `E:\代码` 下：用 Read/Write/Edit 与 `git -C "E:/代码/GraphFlow"` 绝对路径；`commit-graph` warning 无害。
- 跑测试：`cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`（asyncio_mode=auto，fixtures：`client`/`auth_client`/`session_factory`）。前端：`cd frontend && npm run build`（tsc 干净即可）。
- 当前基线 **365 passed**（已恢复并对齐 origin 网关更新）。测试只本地维护、不推 origin。

---

### Task 1：思考 xhigh 硬编码（thinking + factory + compactor + AgentDrawer UI）

**Files:**
- Modify: `backend/app/thinking.py`（加 `force_xhigh`；`agent_chat_settings` 补 reasoning_effort）
- Modify: `backend/app/agent/factory.py:create_model`（进函数即 `force_xhigh`）
- Modify: `backend/app/agent/compactor.py:_default_summarize`（`llm.chat(..., params=force_xhigh(params))`）
- Test: `backend/tests/test_thinking.py`、`backend/tests/test_agent_factory.py`、`backend/tests/test_compactor.py`
- Modify(前端): `frontend/src/agent/AgentDrawer.tsx`（移除分角色思考控件，显示「思考：xhigh（固定）」）

**Interfaces:**
- Produces：`thinking.force_xhigh(params: dict | None) -> dict`，返回 `{**(params or {}), "thinking_enabled": True, "reasoning_effort": "xhigh"}`。
- 变更：`agent_chat_settings` 非 azure 返回 `{"extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": <effort>}}`。

- [ ] **Step 1：写失败测试（thinking）**

在 `backend/tests/test_thinking.py` 末尾追加：

```python
def test_force_xhigh_overrides():
    from app.thinking import force_xhigh
    assert force_xhigh(None) == {"thinking_enabled": True, "reasoning_effort": "xhigh"}
    assert force_xhigh({"thinking_enabled": False, "reasoning_effort": "low", "temperature": 0.3}) == {
        "thinking_enabled": True, "reasoning_effort": "xhigh", "temperature": 0.3}


def test_agent_chat_settings_carries_effort():
    from app.thinking import agent_chat_settings
    assert agent_chat_settings({"reasoning_effort": "xhigh"}) == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}}
```

并把已有的 `test_agent_chat_settings` 第一断言改为带 effort：

```python
def test_agent_chat_settings():
    assert agent_chat_settings({}) == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}}
    assert agent_chat_settings({}, provider="azure") == {}
    assert agent_chat_settings({"thinking_enabled": False}) == {}
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_thinking.py -q -p no:cacheprovider`
Expected: FAIL（`force_xhigh` 不存在；`agent_chat_settings` 无 reasoning_effort）

- [ ] **Step 3：实现 thinking.py**

`backend/app/thinking.py` 顶部默认值后加：

```python
def force_xhigh(params: dict | None) -> dict:
    """RedLotus + 节点助手专用：强制开启思考、力度 xhigh，覆盖任何传入值（保留其余键）。"""
    return {**(params or {}), "thinking_enabled": True, "reasoning_effort": "xhigh"}
```

把 `agent_chat_settings` 改为带 reasoning_effort：

```python
def agent_chat_settings(params: dict | None, *, provider: str = "openai") -> dict:
    if not thinking_enabled(params):
        return {}
    if provider == "azure":
        return {}
    return {"extra_body": {"thinking": {"type": "enabled"},
                           "reasoning_effort": reasoning_effort(params, provider=provider)}}
```

- [ ] **Step 4：跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_thinking.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5：写失败测试（factory 强制 xhigh）**

把 `backend/tests/test_agent_factory.py` 的 `test_create_model_no_key` 与 `test_create_model_thinking_disabled` 改为（强制 xhigh 后思考不可关）：

```python
def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    # 强制 xhigh：agent 路径 extra_body 含 thinking + reasoning_effort=xhigh
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}


def test_create_model_thinking_forced_xhigh_even_if_disabled():
    # 硬编码：即便请求方传 thinking_enabled:false / 低力度，仍强制 xhigh-on
    model = factory.create_model(_mc(), params={"thinking_enabled": False, "reasoning_effort": "low"})
    assert model.settings["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}
```

- [ ] **Step 6：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_agent_factory.py -q -p no:cacheprovider`
Expected: FAIL（当前未强制 xhigh）

- [ ] **Step 7：实现 factory.create_model 强制 xhigh**

`backend/app/agent/factory.py`：先在文件顶部 import 处加入 `force_xhigh`：

```python
from app.thinking import agent_chat_settings, agent_responses_settings, force_xhigh, thinking_enabled
```

`create_model` 函数体第一行改为：

```python
def create_model(mc: ModelConfig, params: dict | None = None) -> OpenAIChatModel | OpenAIResponsesModel:
    default_params = json.loads(mc.default_params_json)
    call_params = force_xhigh(params)        # 批20：RedLotus+助手一律 xhigh，忽略传入思考参数
    merged = {**default_params, **call_params}
```

（其余不变；`use_responses`/`agent_chat_settings`/`agent_responses_settings` 都读 `call_params`，自动得到 xhigh。）

- [ ] **Step 8：跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_agent_factory.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 9：写失败测试（compactor 强制 xhigh）**

新建/追加 `backend/tests/test_compactor.py`（若已存在则追加此用例）：

```python
async def test_default_summarize_forces_xhigh(monkeypatch):
    from app.agent import compactor
    from app.services import llm as llm_mod
    from app import crypto
    from app.models import ModelConfig

    seen = {}

    async def fake_chat(mc, system, user, params=None, retries=3):
        seen["params"] = params
        return "摘要", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    mc = ModelConfig(user_id=1, name="c", model_name="m", base_url="http://x/v1",
                     api_key_enc=crypto.encrypt("sk"), default_params_json="{}")
    out = await compactor._default_summarize(mc, "一些历史文本", params={"thinking_enabled": False})
    assert out == "摘要"
    assert seen["params"]["thinking_enabled"] is True
    assert seen["params"]["reasoning_effort"] == "xhigh"
```

- [ ] **Step 10：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_compactor.py -q -p no:cacheprovider`
Expected: FAIL（compactor 未强制 xhigh）

- [ ] **Step 11：实现 compactor 强制 xhigh**

`backend/app/agent/compactor.py` 的 `_default_summarize`：

```python
async def _default_summarize(compactor_mc, text: str, params: dict | None = None) -> str:
    from app.services import llm
    from app.agent.prompts import load_prompt
    from app.thinking import force_xhigh
    system = load_prompt("compactor_system.md")
    out, _usage = await llm.chat(compactor_mc, system, text, params=force_xhigh(params), retries=2)
    return out
```

- [ ] **Step 12：跑测试确认通过 + 全套回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_compactor.py tests/test_thinking.py tests/test_agent_factory.py -q -p no:cacheprovider`
Expected: PASS。再跑全套确认未回归其它：`python -m pytest -q -p no:cacheprovider`（应仍全绿）。

- [ ] **Step 13：前端移除 AgentDrawer 分角色思考控件**

`frontend/src/agent/AgentDrawer.tsx`：删除 `ThinkingControls` 组件定义与其使用块（约 44-60 行的组件、284-295 行的 `<Space>...ThinkingControls...</Space>`），改为一行只读提示。把 284-295 那段 `<Space ...>{advanced ? ... : <ThinkingControls .../>}</Space>` 整体替换为：

```tsx
        <div style={{ marginBottom: 8, fontSize: 12, color: '#999' }}>思考：xhigh（固定）</div>
```

并删除顶部 `THINKING_EFFORT_OPTIONS`、`ThinkingControls`、以及 `buildSessionPayload` 里 `withThinkingParamDefaults` 注入（保留 `buildSessionPayload` 但 `model_params` 仍可传空对象——后端 create_model 会强制 xhigh）。把 `withThinkingParamDefaults` 调用替换为传 `{}`：

```tsx
  const modelParams = Object.fromEntries(AGENT_ROLES.map((role) => [role, {}]))
```

删除 `roleParams`/`sharedParams` 相关 state 与 setter（不再有分角色思考输入）。如该改动牵涉较多，最小做法：保留 state 但不再渲染 ThinkingControls，`buildSessionPayload` 传 `{}`。

- [ ] **Step 14：前端构建确认**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build`
Expected: tsc 干净、构建成功（无未使用变量报错——若有，删除对应未用 state）。

- [ ] **Step 15：提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/thinking.py backend/app/agent/factory.py backend/app/agent/compactor.py backend/tests/test_thinking.py backend/tests/test_agent_factory.py backend/tests/test_compactor.py frontend/src/agent/AgentDrawer.tsx
git -C "E:/代码/GraphFlow" commit -m "feat(thinking): RedLotus+节点助手思考硬编码 xhigh（force_xhigh 注入 create_model+compactor，agent_chat_settings 补 reasoning_effort，AgentDrawer 移除思考控件）"
```

---

### Task 2：模型日志基础设施（ModelCallLog 表 + log_model_call 切口 + loguru）

**Files:**
- Modify: `backend/app/models.py`（新增 `ModelCallLog`）
- Create: `backend/app/services/model_log.py`（contextvar + log_model_call + 限量 + loguru）
- Modify: `backend/pyproject.toml`（依赖加 `loguru>=0.7`）
- Test: `backend/tests/test_model_log.py`

**Interfaces:**
- Produces：
  - `model_log.log_context(**ctx)` 上下文管理器（设/复位 contextvar）。
  - `model_log.current_ctx() -> dict | None`。
  - `async model_log.log_model_call(*, messages, response_text, ok, model_name, provider, prompt_tokens=0, completion_tokens=0, model_config_id=None)`。
  - `ModelCallLog`（表 `model_call_logs`）。

- [ ] **Step 1：装 loguru**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pip install "loguru>=0.7"`（用跑测试的同一解释器）。并在 `backend/pyproject.toml` 的 `dependencies` 数组加一行 `"loguru>=0.7",`。
Expected: `python -c "import loguru"` 无报错。

- [ ] **Step 2：写失败测试**

新建 `backend/tests/test_model_log.py`：

```python
import json

from sqlalchemy import func, select

from app.models import ModelCallLog
from app.services import model_log


async def _count(session_factory, **w):
    async with session_factory() as s:
        stmt = select(func.count()).select_from(ModelCallLog)
        for k, v in w.items():
            stmt = stmt.where(getattr(ModelCallLog, k) == v)
        return await s.scalar(stmt)


async def test_no_context_no_log(session_factory):
    await model_log.log_model_call(messages=[{"role": "user", "content": "hi"}],
                                   response_text="ok", ok=True, model_name="m", provider="openai")
    assert await _count(session_factory) == 0


async def test_agent_source_full(session_factory):
    with model_log.log_context(user_id=1, session_id=7, source="redlotus"):
        for _ in range(30):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="r", ok=True, model_name="m", provider="openai")
    assert await _count(session_factory, source="redlotus") == 30   # Agent 类全量，不限量


async def test_node_source_success_capped_failures_kept(session_factory):
    with model_log.log_context(user_id=1, run_id=100, node_id="ls", source="synth"):
        for _ in range(25):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="r", ok=True, model_name="m", provider="openai")
        for _ in range(3):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="", ok=False, model_name="m", provider="openai")
    assert await _count(session_factory, source="synth", run_id=100) == model_log.NODE_LIMIT + 3


async def test_redaction_no_secret(session_factory):
    with model_log.log_context(user_id=1, source="redlotus"):
        await model_log.log_model_call(
            messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}],
            response_text="a", ok=True, model_name="m", provider="openai")
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog))).scalars().first()
    blob = row.request_json + row.response_json
    assert "rules" in blob and "api_key" not in blob.lower() and "authorization" not in blob.lower()
```

> 注：`test_node_source_success_capped_failures_kept` 依赖进程内计数；测试用唯一 run_id=100 避免与其它用例串号。

- [ ] **Step 3：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log.py -q -p no:cacheprovider`
Expected: FAIL（`ModelCallLog`/`model_log` 不存在）

- [ ] **Step 4：实现 ModelCallLog 表**

`backend/app/models.py` 末尾追加：

```python
class ModelCallLog(Base):
    __tablename__ = "model_call_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True, default=0)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True, default=None)
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"), index=True, default=None)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("agent_sessions.id"), index=True, default=None)
    node_id: Mapped[str] = mapped_column(default="")
    source: Mapped[str] = mapped_column(default="")  # synth/qc/redlotus/codegen/assistant/compactor
    model_config_id: Mapped[int | None] = mapped_column(default=None)
    model_name: Mapped[str] = mapped_column(default="")
    provider: Mapped[str] = mapped_column(default="")
    request_json: Mapped[str] = mapped_column(Text, default="[]")
    response_json: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

- [ ] **Step 5：实现 model_log.py**

新建 `backend/app/services/model_log.py`：

```python
"""模型/Agent 调用日志：唯一落库切口 + 上下文 contextvar。两条网关共用。
铁律：只记 messages 与响应文本，绝不记 api_key/Authorization 头。"""
import contextlib
import contextvars
import json

from loguru import logger

from app.db import get_session_factory
from app.models import ModelCallLog

NODE_LIMIT = 20                       # 节点类(synth/qc) 每 (run,node) 成功记录上限
_NODE_SOURCES = ("synth", "qc")
_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("model_log_ctx", default=None)
_success_counts: dict[tuple, int] = {}


@contextlib.contextmanager
def log_context(**ctx):
    token = _ctx.set({**(_ctx.get() or {}), **ctx})
    try:
        yield
    finally:
        _ctx.reset(token)


def current_ctx() -> dict | None:
    return _ctx.get()


def _should_log(ctx: dict, ok: bool) -> bool:
    if ctx.get("source") not in _NODE_SOURCES:
        return True                   # Agent 类全量
    if not ok:
        return True                   # 失败行全留
    key = (ctx.get("run_id"), ctx.get("node_id"))
    if _success_counts.get(key, 0) >= NODE_LIMIT:
        return False
    _success_counts[key] = _success_counts.get(key, 0) + 1
    return True


async def log_model_call(*, messages, response_text, ok, model_name, provider,
                         prompt_tokens=0, completion_tokens=0, model_config_id=None):
    ctx = _ctx.get()
    if ctx is None:                   # 无上下文（连通测试/单测）不记
        return
    try:
        if not _should_log(ctx, ok):
            return
        logger.bind(source=ctx.get("source"), run_id=ctx.get("run_id"),
                    node_id=ctx.get("node_id"), ok=ok).info("model_call")
        async with get_session_factory()() as s:
            s.add(ModelCallLog(
                user_id=ctx.get("user_id") or 0, run_id=ctx.get("run_id"),
                workflow_id=ctx.get("workflow_id"), session_id=ctx.get("session_id"),
                node_id=ctx.get("node_id") or "", source=ctx.get("source") or "",
                model_config_id=model_config_id, model_name=model_name, provider=provider,
                request_json=json.dumps(messages, ensure_ascii=False),
                response_json=response_text or "",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))
            await s.commit()
    except Exception as e:            # 记日志失败绝不影响主调用
        logger.warning(f"model_log 落库失败(忽略): {e}")
```

- [ ] **Step 6：跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log.py -q -p no:cacheprovider`
Expected: PASS（4 个用例全绿）

- [ ] **Step 7：提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/models.py backend/app/services/model_log.py backend/pyproject.toml backend/tests/test_model_log.py
git -C "E:/代码/GraphFlow" commit -m "feat(model-log): ModelCallLog 表 + log_model_call 单切口（contextvar/节点类限量/脱敏/loguru）"
```

---

### Task 3：两条网关接入日志（llm.chat + LoggingModel + 各 set-point）

**Files:**
- Modify: `backend/app/services/llm.py:chat`（成功/重试耗尽前调 log_model_call）
- Create: `backend/app/agent/logging_model.py`（WrapperModel 包一层）
- Modify: `backend/app/agent/factory.py:create_model`（返回前包 LoggingModel）
- Modify: `backend/app/engine/runner.py`（synth/qc/regen 调用点设 log_context）
- Modify: `backend/app/agent/turns.py`（_run_turn/_run_goal 设 log_context source="redlotus"）
- Modify: `backend/app/routers/agent.py`（codegen/node-assist 端点设 log_context）
- Test: `backend/tests/test_model_log_gateway.py`

**Interfaces:**
- Consumes：`model_log.log_context/log_model_call/current_ctx`（Task 2）。
- Produces：`logging_model.LoggingModel(wrapped)`（pydantic-ai Model）。

- [ ] **Step 1：写失败测试（节点网关）**

新建 `backend/tests/test_model_log_gateway.py`：

```python
from types import SimpleNamespace

from sqlalchemy import select

from app.models import ModelConfig, ModelCallLog
from app.services import llm, model_log


def _mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen", base_url="http://x/v1",
                       api_key_enc=crypto.encrypt("sk-1"), default_params_json="{}")


def _fake_resp(text="好"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))


async def test_chat_logs_with_context(session_factory, monkeypatch):
    async def create(**kw):
        return _fake_resp()
    monkeypatch.setattr(llm, "_client",
                        lambda _: SimpleNamespace(chat=SimpleNamespace(
                            completions=SimpleNamespace(create=create))))
    with model_log.log_context(user_id=1, run_id=5, node_id="ls", source="synth"):
        await llm.chat(_mc(), "系统", "用户")
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog))).scalars().first()
    assert row is not None and row.source == "synth" and row.node_id == "ls"
    assert "用户" in row.request_json and "好" in row.response_json
    assert "sk-1" not in row.request_json   # 不泄露密钥


async def test_chat_no_context_no_log(session_factory, monkeypatch):
    async def create(**kw):
        return _fake_resp()
    monkeypatch.setattr(llm, "_client",
                        lambda _: SimpleNamespace(chat=SimpleNamespace(
                            completions=SimpleNamespace(create=create))))
    await llm.chat(_mc(), "", "u")        # 无上下文
    async with session_factory() as s:
        assert (await s.execute(select(ModelCallLog))).scalars().first() is None
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log_gateway.py -q -p no:cacheprovider`
Expected: FAIL（chat 未落日志）

- [ ] **Step 3：实现 llm.chat 接入日志**

`backend/app/services/llm.py`：顶部 import 加 `from app.services.model_log import log_model_call`。`chat()` 内：在成功 `return content, usage` 前、以及最后 `raise LLMError` 前各记一条。把 try/except 循环改为：

```python
    client = _client(mc)
    provider = provider_name(mc)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=mc.model_name, messages=messages,
                timeout=merged.get("timeout", 120), **kwargs)
            content = resp.choices[0].message.content or ""
            if not content.strip():
                raise LLMError("模型返回空内容")
            usage = {"prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                     "completion_tokens": resp.usage.completion_tokens if resp.usage else 0}
            await log_model_call(messages=messages, response_text=content, ok=True,
                                 model_name=mc.model_name, provider=provider,
                                 prompt_tokens=usage["prompt_tokens"],
                                 completion_tokens=usage["completion_tokens"], model_config_id=mc.id)
            return content, usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    await log_model_call(messages=messages, response_text=f"[失败] {last_err}", ok=False,
                         model_name=mc.model_name, provider=provider, model_config_id=mc.id)
    raise LLMError(str(last_err))
```

（`provider_name` 已从 `app.llm_clients` 导入；若未导入则在顶部 import 补上。）

- [ ] **Step 4：跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log_gateway.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5：写失败测试（Agent 网关 LoggingModel）**

在 `backend/tests/test_model_log_gateway.py` 追加：

```python
async def test_logging_model_wraps_agent_run(session_factory):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from app.agent.logging_model import LoggingModel

    agent = Agent(LoggingModel(TestModel(custom_output_text="你好")))
    with model_log.log_context(user_id=1, session_id=9, source="redlotus"):
        result = await agent.run("hi")
    assert "你好" in str(result.output)
    async with session_factory() as s:
        rows = (await s.execute(select(ModelCallLog).where(ModelCallLog.source == "redlotus"))).scalars().all()
    assert len(rows) >= 1 and "hi" in rows[0].request_json and "你好" in rows[0].response_json
```

- [ ] **Step 6：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log_gateway.py::test_logging_model_wraps_agent_run -q -p no:cacheprovider`
Expected: FAIL（`logging_model` 不存在）

- [ ] **Step 7：实现 LoggingModel**

新建 `backend/app/agent/logging_model.py`：

```python
"""Agent 路径模型日志网关：包一层 WrapperModel，request/request_stream 返回后落库。
经 factory.create_model 统一套上，覆盖所有 agent（coordinator/manager/worker/codegen/节点助手）。"""
from contextlib import asynccontextmanager

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.wrapper import WrapperModel

from app.services.model_log import current_ctx, log_model_call


def _texts(parts) -> str:
    return "\n".join(p.content for p in parts
                     if isinstance(getattr(p, "content", None), str))


def _dump(messages) -> list:
    try:
        return ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    except Exception:
        return [{"kind": getattr(m, "kind", "?"), "text": _texts(getattr(m, "parts", []))}
                for m in messages]


class LoggingModel(WrapperModel):
    async def request(self, messages, model_settings, model_request_parameters):
        resp = await super().request(messages, model_settings, model_request_parameters)
        await self._log(messages, resp)
        return resp

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with super().request_stream(messages, model_settings,
                                          model_request_parameters, run_context) as rs:
            yield rs
        await self._log(messages, rs.get())

    async def _log(self, messages, resp):
        if current_ctx() is None:
            return
        usage = getattr(resp, "usage", None)
        await log_model_call(
            messages=_dump(messages), response_text=_texts(getattr(resp, "parts", [])),
            ok=True, model_name=self.model_name, provider=(self.system or ""),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0)
```

- [ ] **Step 8：create_model 返回前包 LoggingModel**

`backend/app/agent/factory.py`：import 加 `from app.agent.logging_model import LoggingModel`。`create_model` 末尾 `return` 改为：

```python
    model = model_cls(
        mc.model_name,
        provider=make_agent_provider(mc, responses=use_responses),
        settings=ModelSettings(**kw) if kw else None,
    )
    return LoggingModel(model)
```

- [ ] **Step 9：跑测试确认通过 + 全套回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_log_gateway.py tests/test_agent_factory.py -q -p no:cacheprovider`
Expected: PASS（注意：`test_agent_factory` 的 `model.settings`/`model.model_name` 经 LoggingModel 委托仍可访问；若某断言因包装失败，确认走 `WrapperModel` 委托而非 isinstance）。再 `python -m pytest -q -p no:cacheprovider` 全套确认绿。

- [ ] **Step 10：接入各 set-point（runner / turns / agent 路由）**

`backend/app/engine/runner.py`：import 加 `from app.services.model_log import log_context`。
- `_run_llm_node.work()` 里把对 `nodes.run_llm_synth_row` 的调用包进上下文：

```python
            try:
                with log_context(run_id=run_id, node_id=node.id, user_id=user_id, source="synth"):
                    out_rows, usage = await _cancellable(
                        nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem), cancel_event)
```

- `_run_qc_node` 的 `judge()` 内对 `nodes.run_qc_judge_row` 同样包裹（source="qc"）：

```python
            async with sem:
                with log_context(run_id=run_id, node_id=node.id, user_id=user_id, source="qc"):
                    return await _cancellable(
                        nodes.run_qc_judge_row(cfg, row, jmcs, pass_k, user_sem), cancel_event)
```

- `_run_qc_node` 的 `regen()` 内对回扫 `run_llm_synth_row` 包裹（source="synth"，node_id 用回扫目标 `target_id`）：

```python
            async with rsem:
                with log_context(run_id=run_id, node_id=target_id, user_id=user_id, source="synth"):
                    return await _cancellable(
                        nodes.run_llm_synth_row(regen_cfg, row, tmc, user_sem), cancel_event)
```

> 说明：`with log_context(...)` 是同步设置 contextvar；其内 `await` 与 `run_llm_synth_row` 内 `asyncio.create_task(one())`/`asyncio.gather` 创建的子任务都会捕获该上下文快照，故 fanout 子调用也带正确 ctx。

`backend/app/agent/turns.py`：import 加 `from app.services.model_log import log_context`。在 `_run_turn` 与 `_run_goal` 里把 `try: while True: ...` 整段包进：

```python
        with log_context(user_id=user_id, session_id=session_id, source="redlotus"):
            try:
                while True:
                    ...
```

（缩进相应内移；`EMIT.set` 已证明 contextvar 能传到 worker 子任务，故 source=redlotus 覆盖 coordinator/manager/worker。）

`backend/app/routers/agent.py`：import 加 `from app.services.model_log import log_context`。`codegen` 端点把 `generate_code(...)` 调用包进 `with log_context(user_id=user.id, workflow_id=body.workflow_id, node_id=body.node_id, source="codegen"):`；`node_assist` 端点把 `generate_node_config(...)` 调用包进 `with log_context(user_id=user.id, workflow_id=body.workflow_id, node_id=body.node_id, source="assistant"):`。

- [ ] **Step 11：写 set-point 集成测试**

在 `backend/tests/test_model_log_gateway.py` 追加（用现有 `auth_client`/`session_factory` + 假 chat 跑一个最小 synth run 不现实，故改测 node-assist 端点经 LoggingModel 落库——但 node-assist 用真实 create_agent，单测可改为断言 source 标签经 log_context 生效）：

```python
async def test_node_assist_logs_source_assistant(auth_client, session_factory, monkeypatch):
    from app.routers import agent as agent_router

    async def fake_cfg(model, node_type, instruction, columns, current_config=None,
                       preview_tools=None, params=None, history=None):
        # 模拟 generate_node_config 内部对网关的一次记录
        from app.services.model_log import current_ctx, log_model_call
        assert current_ctx()["source"] == "assistant"
        await log_model_call(messages=[{"role": "user", "content": instruction}],
                             response_text="ok", ok=True, model_name="m", provider="openai")
        return {"reply": "已生成", "config": {"system_prompt": "s", "user_prompt": "u"}}

    monkeypatch.setattr(agent_router.codegen_mod, "generate_node_config", fake_cfg)
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "x", "base_url": "http://x", "api_key": "k"})).json()
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": mc["id"]})
    assert r.status_code == 200
    from sqlalchemy import select
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog).where(ModelCallLog.source == "assistant"))).scalars().first()
    assert row is not None and row.workflow_id == wf["id"] and row.node_id == "ls"
```

> 该测试同时验证 Task 5 将给 `generate_node_config` 增加的 `history` 参数与新返回 `{reply, config}`；在 Task 3 阶段 `generate_node_config` 尚未改签名，故此用例放到 Task 5 实现后再启用——**在 Task 3 先只跑前述节点/Agent 网关用例**，本用例标 `@pytest.mark.skip(reason="待 Task5 node-assist 多轮")` 占位，Task 5 去掉 skip。

- [ ] **Step 12：跑测试 + 全套回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`
Expected: 全绿（含已恢复的 e2e：经 LoggingModel 包裹后 coordinator/goal 流仍正常，且 source=redlotus 落库不影响断言）。

- [ ] **Step 13：提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/services/llm.py backend/app/agent/logging_model.py backend/app/agent/factory.py backend/app/engine/runner.py backend/app/agent/turns.py backend/app/routers/agent.py backend/tests/test_model_log_gateway.py
git -C "E:/代码/GraphFlow" commit -m "feat(model-log): 两条网关接入（llm.chat + LoggingModel 包 create_model）+ runner/turns/codegen/node-assist set-point"
```

---

### Task 4：日志查询端点 + 级联删除

**Files:**
- Create: `backend/app/routers/model_logs.py`（`GET /api/model-logs`）
- Modify: `backend/app/routers/runs.py`（`GET /api/runs/{id}/model-logs`；delete_run/delete_all_runs 级联删 ModelCallLog）
- Modify: `backend/app/routers/workflows.py`（delete_workflow 级联删 ModelCallLog）
- Modify: `backend/app/routers/agent.py`（delete_session/delete_all_sessions 级联删 ModelCallLog）
- Modify: `backend/app/main.py`（include model_logs.router）
- Test: `backend/tests/test_model_logs_api.py`

**Interfaces:**
- Produces：`GET /api/model-logs?source=&run_id=&node_id=&limit=&offset=`（仅本人）→ `[{id, source, node_id, run_id, model_name, request, response, prompt_tokens, completion_tokens, created_at}]`；`GET /api/runs/{run_id}/model-logs` 同形（限该 run）。

- [ ] **Step 1：写失败测试**

新建 `backend/tests/test_model_logs_api.py`：

```python
from app.models import ModelCallLog


async def _seed(session_factory, **kw):
    async with session_factory() as s:
        s.add(ModelCallLog(request_json='[{"role":"user","content":"q"}]',
                           response_json="a", model_name="m", provider="openai", **kw))
        await s.commit()


async def test_list_model_logs_isolated_and_filtered(auth_client, session_factory):
    me = (await auth_client.get("/api/me")).json()["id"]
    await _seed(session_factory, user_id=me, source="synth", run_id=5, node_id="ls")
    await _seed(session_factory, user_id=me, source="redlotus", session_id=3)
    await _seed(session_factory, user_id=999999, source="synth", run_id=5)  # 他人
    r = await auth_client.get("/api/model-logs")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2                       # 不含他人
    r2 = await auth_client.get("/api/model-logs?source=synth")
    assert [x["source"] for x in r2.json()] == ["synth"]
    assert r2.json()[0]["request"] == [{"role": "user", "content": "q"}]


async def test_run_model_logs_scoped(auth_client, session_factory):
    me = (await auth_client.get("/api/me")).json()["id"]
    from app.models import Run, WorkflowVersion, Workflow
    async with session_factory() as s:
        wf = Workflow(user_id=me, name="w"); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}"); s.add(ver); await s.flush()
        run = Run(user_id=me, workflow_id=wf.id, workflow_version_id=ver.id); s.add(run)
        await s.commit(); rid = run.id
    await _seed(session_factory, user_id=me, source="synth", run_id=rid, node_id="ls")
    r = await auth_client.get(f"/api/runs/{rid}/model-logs")
    assert r.status_code == 200 and len(r.json()) == 1


async def test_delete_run_cascades_model_logs(auth_client, session_factory):
    from sqlalchemy import func, select
    from app.models import Run, WorkflowVersion, Workflow
    me = (await auth_client.get("/api/me")).json()["id"]
    async with session_factory() as s:
        wf = Workflow(user_id=me, name="w"); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}"); s.add(ver); await s.flush()
        run = Run(user_id=me, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.commit(); rid = run.id
    await _seed(session_factory, user_id=me, source="synth", run_id=rid)
    await auth_client.delete(f"/api/runs/{rid}")
    async with session_factory() as s:
        n = await s.scalar(select(func.count()).select_from(ModelCallLog).where(ModelCallLog.run_id == rid))
    assert n == 0
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_logs_api.py -q -p no:cacheprovider`
Expected: FAIL（端点不存在）

- [ ] **Step 3：实现 model_logs 路由**

新建 `backend/app/routers/model_logs.py`：

```python
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import ModelCallLog, User

router = APIRouter(prefix="/api/model-logs", tags=["model-logs"])


def _out(r: ModelCallLog) -> dict:
    return {"id": r.id, "source": r.source, "node_id": r.node_id, "run_id": r.run_id,
            "workflow_id": r.workflow_id, "session_id": r.session_id,
            "model_name": r.model_name, "provider": r.provider,
            "request": json.loads(r.request_json or "[]"), "response": r.response_json,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "created_at": r.created_at.isoformat()}


@router.get("")
async def list_model_logs(source: str | None = None, run_id: int | None = None,
                          node_id: str | None = None, limit: int = 100, offset: int = 0,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    stmt = select(ModelCallLog).where(ModelCallLog.user_id == user.id)
    if source is not None:
        stmt = stmt.where(ModelCallLog.source == source)
    if run_id is not None:
        stmt = stmt.where(ModelCallLog.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(ModelCallLog.node_id == node_id)
    rows = (await session.execute(
        stmt.order_by(ModelCallLog.id.desc()).offset(offset).limit(min(limit, 500)))).scalars().all()
    return [_out(r) for r in rows]
```

`backend/app/main.py`：import 加 `model_logs`，`create_app` 里 `app.include_router(model_logs.router)`。

- [ ] **Step 4：实现 run 维度端点 + 级联**

`backend/app/routers/runs.py`：
- import 的 models 元组加入 `ModelCallLog`。
- 加端点（放在 `run_logs` 附近）：

```python
@router.get("/{run_id}/model-logs")
async def run_model_logs(run_id: int, node_id: str | None = None, source: str | None = None,
                         limit: int = 200, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    stmt = select(ModelCallLog).where(ModelCallLog.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(ModelCallLog.node_id == node_id)
    if source is not None:
        stmt = stmt.where(ModelCallLog.source == source)
    rows = (await session.execute(stmt.order_by(ModelCallLog.id.desc()).limit(min(limit, 500)))).scalars().all()
    from app.routers.model_logs import _out
    return [_out(r) for r in rows]
```

- `delete_run`：在删 `RunRow...` 那批里加 `await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.run_id == run_id))`。
- `delete_all_runs`：把 `for Model in (RunRow, RunNodeState, RunLog, QcMetric, QcFailure):` 改为含 `ModelCallLog`。

`backend/app/routers/workflows.py`：import 加 `ModelCallLog`；`delete_workflow` 里 run_ids 级联那批加 `ModelCallLog`（按 run_id），并对 node-assist 类（无 run、有 workflow_id）补一条 `await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.workflow_id == wf.id))`。

`backend/app/routers/agent.py`：import 加 `ModelCallLog`；`delete_session` 加 `await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.session_id == sid))`；`delete_all_sessions` 在 sids 非空分支加 `await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.session_id.in_(sids)))`。

- [ ] **Step 5：跑测试确认通过 + 全套回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_model_logs_api.py -q -p no:cacheprovider` → PASS。
再 `python -m pytest -q -p no:cacheprovider` 全绿。

- [ ] **Step 6：提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/routers/model_logs.py backend/app/routers/runs.py backend/app/routers/workflows.py backend/app/routers/agent.py backend/app/main.py backend/tests/test_model_logs_api.py
git -C "E:/代码/GraphFlow" commit -m "feat(model-log): /api/model-logs + /api/runs/{id}/model-logs 查询（用户隔离）+ run/workflow/session 级联删除"
```

---

### Task 5：节点助手后端改多轮（history + {reply, config}）

**Files:**
- Modify: `backend/app/agent/codegen.py:generate_node_config`（加 `history`，返回 `{reply, config}`）
- Modify: `backend/app/routers/agent.py`（NodeAssistIn 加 `history`；端点返回 `{reply, config, sample_source}`）
- Modify: `backend/app/agent/prompts/node_assist_llm_synth.md`、`node_assist_qc.md`（输出契约改 `{reply, config}`）
- Test: `backend/tests/test_node_assist_multiturn.py`；启用 Task3 的 skip 用例

**Interfaces:**
- 变更：`generate_node_config(model, node_type, instruction, columns, current_config=None, preview_tools=None, params=None, history=None) -> {"reply": str, "config": dict | None}`。
- 端点返回 `{"reply": str, "config": dict | None, "sample_source": str}`。

- [ ] **Step 1：写失败测试**

新建 `backend/tests/test_node_assist_multiturn.py`：

```python
import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from app.agent import codegen


async def test_generate_node_config_returns_reply_and_config():
    out = json.dumps({"reply": "已按需求生成翻译配置", "config": {
        "system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}}, ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```json\n{out}\n```")]))
    r = await codegen.generate_node_config(model, "llm_synth", "把 q 翻译成英文", ["q"])
    assert r["reply"] == "已按需求生成翻译配置"
    assert r["config"]["output_column"] == "q_en"


async def test_generate_node_config_passes_history():
    seen = {}

    def fn(messages, info):
        seen["n_user"] = sum(1 for m in messages if isinstance(m, ModelRequest)
                             for p in m.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[TextPart('{"reply":"好","config":null}')])

    history = [{"role": "user", "text": "第一轮"}, {"role": "assistant", "text": "回应"}]
    r = await codegen.generate_node_config(FunctionModel(fn), "qc", "再严格点", ["q"], history=history)
    assert r["config"] is None and r["reply"] == "好"
    assert seen["n_user"] == 2   # 历史里 1 条 user + 本轮 instruction 1 条
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_node_assist_multiturn.py -q -p no:cacheprovider`
Expected: FAIL

- [ ] **Step 3：实现 generate_node_config 多轮**

`backend/app/agent/codegen.py`：顶部 import 加 `from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart`。改 `generate_node_config`：

```python
def _to_history(history: list[dict] | None) -> list:
    msgs = []
    for h in history or []:
        if h.get("role") == "user":
            msgs.append(ModelRequest(parts=[UserPromptPart(content=h.get("text", ""))]))
        else:
            msgs.append(ModelResponse(parts=[TextPart(content=h.get("text", ""))]))
    return msgs


async def generate_node_config(model, node_type: str, instruction: str, columns: list[str],
                               current_config: dict | None = None,
                               preview_tools: list | None = None,
                               params: dict | None = None,
                               history: list[dict] | None = None) -> dict:
    """多轮：带 history 跑一轮，返回 {reply, config}。config 为 None 表示本轮只对话不产配置。
    未知 node_type 抛 KeyError。"""
    agent = create_agent(model, preview_tools or [], NODE_ASSIST_INSTRUCTIONS[node_type], params=params)
    prompt = _user_prompt(instruction, columns)
    if current_config:
        prompt += ("\n\n现有节点配置（请在此基础上增量修改，保留已有提示词中的处理，"
                   "不要丢失之前的需求）：\n" + json.dumps(current_config, ensure_ascii=False))
    result = await agent.run(prompt, message_history=_to_history(history))
    data = json.loads(strip_code_fences(str(result.output or "")))
    return {"reply": data.get("reply", ""), "config": data.get("config")}
```

- [ ] **Step 4：跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest tests/test_node_assist_multiturn.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5：改提示词输出契约**

`backend/app/agent/prompts/node_assist_llm_synth.md` 与 `node_assist_qc.md`：在文件开头加输出契约（保留原有"如何写好该节点配置"的指引正文，仅改"只输出什么"）：

```
你是 GraphFlow 节点配置助手，与用户多轮对话，帮其配置该节点。
只输出一个 JSON 对象，不要任何解释或 markdown 围栏：
{"reply": "<给用户看的中文回应>", "config": <节点配置对象 或 null>}
- reply：始终填写，简述你这轮做了什么/还需用户澄清什么。
- config：当你给出一份可应用的节点配置时填该对象；只是答疑/追问时填 null。
config 对象的字段要求见下文（沿用原节点配置规范）。
```

（其后保留各自原有的字段规范正文。）

- [ ] **Step 6：改 node-assist 端点（history + 新返回）**

`backend/app/routers/agent.py`：`NodeAssistIn` 加 `history: list[dict] = []`；端点：

```python
@router.post("/node-assist")
async def node_assist(body: NodeAssistIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    if body.node_type not in ("llm_synth", "qc"):
        raise HTTPException(status_code=422, detail="该节点类型不支持助手")
    wf = await session.get(Workflow, body.workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    mc = await session.get(ModelConfig, body.model_config_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=422, detail="模型配置无效")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="指令不能为空")
    columns, source = await gather_upstream_columns(session, body.workflow_id, body.node_id, user.id)
    preview_tools = make_preview_tools(get_session_factory(), user.id,
                                       workflow_id=body.workflow_id, node_id=body.node_id)
    try:
        with log_context(user_id=user.id, workflow_id=body.workflow_id,
                         node_id=body.node_id, source="assistant"):
            r = await codegen_mod.generate_node_config(
                mc, body.node_type, body.instruction, columns, current_config=body.current_config,
                preview_tools=preview_tools, params=body.params, history=body.history)
    except ModelHTTPError as exc:
        _raise_model_http_error(exc, mc)
    return {"reply": r["reply"], "config": r["config"], "sample_source": source}
```

- [ ] **Step 7：启用 Task3 占位用例 + 修旧断言**

去掉 `backend/tests/test_model_log_gateway.py::test_node_assist_logs_source_assistant` 的 `@pytest.mark.skip`（其 fake_cfg 签名已含 `history=None`）。
更新 `backend/tests/test_agent_api.py::test_node_assist_guards`：fake_cfg 返回改为 `{"reply": "好", "config": {"system_prompt": "s", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}}`，断言改 `r.json()["config"]["output_column"] == "q_en"`。
更新 `backend/tests/test_assistant_context.py::test_node_assist_endpoint_passes_current_config`：fake_cfg 加 `history=None` 形参、返回 `{"reply": "ok", "config": {...}}`（断言 current_config 不变）。

- [ ] **Step 8：跑测试 + 全套回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`
Expected: 全绿。

- [ ] **Step 9：提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/agent/codegen.py backend/app/routers/agent.py backend/app/agent/prompts/node_assist_llm_synth.md backend/app/agent/prompts/node_assist_qc.md backend/tests/test_node_assist_multiturn.py backend/tests/test_model_log_gateway.py backend/tests/test_agent_api.py backend/tests/test_assistant_context.py
git -C "E:/代码/GraphFlow" commit -m "feat(node-assist): 节点助手改无状态多轮（history 透传+返回 {reply,config}），提示词输出契约更新"
```

---

### Task 6：前端节点助手 store + 草稿 + 多轮 UI

**Files:**
- Create: `frontend/src/agent/nodeAssistantStore.ts`（模块级 store + useSyncExternalStore）
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`NodeAssist` 改多轮聊天 UI，用 store）
- Modify: `frontend/src/api/types.ts`（`NodeAssistReply`）
- Test: 手验 + `npm run build`

**Interfaces:**
- Consumes：`POST /api/agent/node-assist`（Task 5：含 `history`，返回 `{reply, config, sample_source}`）。
- Produces：`useNodeAssist(key)`、`setDraft(key, text)`、`sendAssist(key, payload)`，`key = `${workflowId}:${nodeId}``。

- [ ] **Step 1：加类型**

`frontend/src/api/types.ts` 追加：

```ts
export interface NodeAssistReply {
  reply: string
  config: Record<string, any> | null
  sample_source: 'computed' | 'latest_run' | 'dataset' | 'none'
}
```

- [ ] **Step 2：实现 store**

新建 `frontend/src/agent/nodeAssistantStore.ts`：

```ts
import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { NodeAssistReply } from '../api/types'

export interface AssistMsg { role: 'user' | 'assistant'; text: string; config?: Record<string, any> }
export interface NodeAssistState { messages: AssistMsg[]; draft: string; pending: boolean }

const EMPTY: NodeAssistState = { messages: [], draft: '', pending: false }
const states = new Map<string, NodeAssistState>()
const listeners = new Set<() => void>()

function emit() { listeners.forEach((l) => l()) }
function get(key: string): NodeAssistState { return states.get(key) ?? EMPTY }
function set(key: string, next: NodeAssistState) { states.set(key, next); emit() }

export function useNodeAssist(key: string): NodeAssistState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l) } },
    () => states.get(key) ?? EMPTY,
  )
}

export function setDraft(key: string, draft: string) { set(key, { ...get(key), draft }) }

export async function sendAssist(key: string, payload: {
  workflow_id: number; node_id: string; node_type: string; model_config_id: number
  current_config: Record<string, any>; params: Record<string, any>
}) {
  const cur = get(key)
  const text = cur.draft.trim()
  if (!text || cur.pending) return
  const history = cur.messages.map((m) => ({ role: m.role, text: m.text }))
  set(key, { messages: [...cur.messages, { role: 'user', text }], draft: '', pending: true })
  try {
    const r = await api.post<NodeAssistReply>('/api/agent/node-assist', { ...payload, instruction: text, history })
    const c = get(key)
    set(key, { ...c, pending: false,
      messages: [...c.messages, { role: 'assistant', text: r.reply, config: r.config ?? undefined }] })
  } catch (e) {
    const c = get(key)
    set(key, { ...c, pending: false,
      messages: [...c.messages, { role: 'assistant', text: '出错：' + (e as Error).message }] })
  }
}
```

> 说明：store 在模块级（应用生命周期），不随节点抽屉/页面卸载而清空 → 切节点/换页不丢草稿与历史；`sendAssist` 的 promise 由 store 持有，组件卸载后 resolve 仍回填该 key → 「后台继续」。整页刷新（F5）丢失，符合既定取舍。

- [ ] **Step 3：NodeAssist 组件改多轮聊天**

`frontend/src/canvas/forms/NodeConfigForm.tsx`：把 `NodeAssist` 组件整体替换为（保留 props 形态，新增按 store 渲染对话 + 「应用到节点」按钮）：

```tsx
function NodeAssist({ nodeType, workflowId, nodeId, config, onApply }: {
  nodeType: string; workflowId?: number; nodeId?: string
  config: Record<string, any>
  onApply: (config: Record<string, any>) => void
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const key = `${workflowId ?? 0}:${nodeId ?? ''}`
  const st = useNodeAssist(key)
  useEffect(() => { void api.get<ModelConfig[]>('/api/models').then(setModels) }, [])
  const send = () => {
    if (!modelSel || !workflowId || !nodeId) return
    void sendAssist(key, {
      workflow_id: workflowId, node_id: nodeId, node_type: nodeType, model_config_id: modelSel,
      current_config: config, params: withThinkingParamDefaults(config.params),
    })
  }
  return (
    <div style={{ border: '1px dashed #d9d9d9', borderRadius: 6, padding: 8, marginBottom: 12 }}>
      <div style={{ color: '#722ed1', marginBottom: 4 }}>RedLotus 助手：多轮对话配置本节点</div>
      <div style={{ maxHeight: 200, overflowY: 'auto', marginBottom: 8 }}>
        {st.messages.map((m, i) => (
          <div key={i} style={{ textAlign: m.role === 'user' ? 'right' : 'left', margin: '4px 0' }}>
            <span style={{ background: m.role === 'user' ? '#e6f4ff' : '#f6ffed',
                           borderRadius: 8, padding: '4px 8px', display: 'inline-block',
                           whiteSpace: 'pre-wrap', fontSize: 12 }}>{m.text}</span>
            {m.config && (
              <div><Button size="small" type="link"
                           onClick={() => onApply(m.config!)}>应用到节点</Button></div>
            )}
          </div>
        ))}
        {st.pending && <Spin size="small" style={{ display: 'block', margin: 4 }} />}
      </div>
      <Input.TextArea rows={2} value={st.draft} placeholder="如：把 q 列翻译成英文存到 q_en；再严格点…"
                      onChange={(e) => setDraft(key, e.target.value)} />
      <Space style={{ marginTop: 8 }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={setModelSel} options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={st.pending} disabled={!st.draft.trim() || !modelSel}
                onClick={send}>发送</Button>
      </Space>
    </div>
  )
}
```

文件顶部 import 加：`import { Spin } from 'antd'`（合并进现有 antd import）；`import { useNodeAssist, setDraft, sendAssist } from '../../agent/nodeAssistantStore'`。

- [ ] **Step 4：构建确认 + 手验**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build`
Expected: tsc 干净、构建成功。手验：打开节点配置抽屉，在助手输入框打字→切到另一节点→切回，草稿与对话仍在；发送后草稿清空、对话追加；助手给出 config 的消息下出现「应用到节点」，点了才改配置；F5 后清空（预期）。

- [ ] **Step 5：提交**

```bash
git -C "E:/代码/GraphFlow" add frontend/src/agent/nodeAssistantStore.ts frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/api/types.ts
git -C "E:/代码/GraphFlow" commit -m "feat(node-assist): 前端多轮助手（应用级 store 按 node 隔离会话+草稿，切节点/换页不丢、后台续、应用按钮）"
```

---

### Task 7：模型日志前端（全局页 + 导航 + run 详情 Tab）

**Files:**
- Create: `frontend/src/pages/ModelLogsPage.tsx`
- Modify: `frontend/src/App.tsx`（路由 `/model-logs` + 侧栏菜单项）
- Modify: `frontend/src/pages/RunDetailPage.tsx`（加「模型对话」Tab）
- Modify: `frontend/src/api/types.ts`（`ModelLogEntry`）

**Interfaces:**
- Consumes：`GET /api/model-logs?...`、`GET /api/runs/{id}/model-logs`（Task 4）。

- [ ] **Step 1：加类型**

`frontend/src/api/types.ts` 追加：

```ts
export interface ModelLogEntry {
  id: number; source: string; node_id: string; run_id: number | null
  workflow_id: number | null; session_id: number | null
  model_name: string; provider: string
  request: { role: string; content: string }[] | any
  response: string; prompt_tokens: number; completion_tokens: number; created_at: string
}
```

- [ ] **Step 2：全局日志页**

新建 `frontend/src/pages/ModelLogsPage.tsx`：

```tsx
import { useEffect, useState } from 'react'
import { Card, Select, Space, Table, Tag } from 'antd'
import { api } from '../api/client'
import type { ModelLogEntry } from '../api/types'

const SOURCES = ['', 'synth', 'qc', 'redlotus', 'assistant', 'codegen', 'compactor']

export default function ModelLogsPage() {
  const [source, setSource] = useState('')
  const [rows, setRows] = useState<ModelLogEntry[]>([])
  useEffect(() => {
    void api.get<ModelLogEntry[]>(`/api/model-logs${source ? `?source=${source}` : ''}`).then(setRows)
  }, [source])
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <span>来源</span>
        <Select style={{ width: 160 }} value={source} onChange={setSource}
                options={SOURCES.map((s) => ({ value: s, label: s || '全部' }))} />
      </Space>
      <Table rowKey="id" dataSource={rows} size="small" pagination={{ pageSize: 20 }}
             expandable={{ expandedRowRender: (r) => (
               <Card size="small">
                 <div style={{ fontWeight: 600 }}>请求</div>
                 <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(r.request, null, 2)}</pre>
                 <div style={{ fontWeight: 600 }}>响应</div>
                 <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{r.response}</pre>
               </Card>
             ) }}
             columns={[
               { title: '来源', dataIndex: 'source', render: (s: string) => <Tag>{s}</Tag> },
               { title: '节点', dataIndex: 'node_id' },
               { title: 'run', dataIndex: 'run_id' },
               { title: '模型', dataIndex: 'model_name' },
               { title: 'tokens', render: (_: unknown, r: ModelLogEntry) => r.prompt_tokens + r.completion_tokens },
               { title: '时间', dataIndex: 'created_at' },
             ]} />
    </>
  )
}
```

- [ ] **Step 3：路由 + 菜单**

`frontend/src/App.tsx`：import 加 `import ModelLogsPage from './pages/ModelLogsPage'`；菜单 items 在「运行记录」后加 `{ key: '/model-logs', label: <Link to="/model-logs">模型日志</Link> }`；Routes 加 `<Route path="/model-logs" element={<ModelLogsPage />} />`。

- [ ] **Step 4：run 详情加 Tab**

`frontend/src/pages/RunDetailPage.tsx`：加 state + 拉取：

```tsx
  const [modelLogs, setModelLogs] = useState<ModelLogEntry[]>([])
  useEffect(() => {
    if (!run || isActive) return
    void api.get<ModelLogEntry[]>(`/api/runs/${id}/model-logs`).then(setModelLogs)
  }, [run?.status, id, isActive])
```

在 `<Tabs items={[...]}>` 数组里追加一个 Tab：

```tsx
              {
                key: 'modellog', label: `模型对话（${modelLogs.length}）`,
                children: <Table rowKey="id" dataSource={modelLogs} size="small"
                                 pagination={{ pageSize: 10 }}
                                 expandable={{ expandedRowRender: (r) => (
                                   <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
{JSON.stringify(r.request, null, 2)}{'\n--- 响应 ---\n'}{r.response}</pre>
                                 ) }}
                                 columns={[
                                   { title: '来源', dataIndex: 'source' },
                                   { title: '节点', dataIndex: 'node_id' },
                                   { title: '模型', dataIndex: 'model_name' },
                                 ]} />,
              },
```

`RunDetailPage.tsx` 顶部 type import 加 `ModelLogEntry`。

- [ ] **Step 5：构建确认**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build`
Expected: tsc 干净、构建成功。

- [ ] **Step 6：提交**

```bash
git -C "E:/代码/GraphFlow" add frontend/src/pages/ModelLogsPage.tsx frontend/src/App.tsx frontend/src/pages/RunDetailPage.tsx frontend/src/api/types.ts
git -C "E:/代码/GraphFlow" commit -m "feat(model-log): 前端全局模型日志页 + 侧栏入口 + run 详情「模型对话」Tab"
```

---

### Task 8：节点配置折叠布局（全分组、默认全折叠）

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（各子表单字段收进 AntD `Collapse`）

**Interfaces:** 纯前端，无后端依赖。

- [ ] **Step 1：LlmSynthForm 折叠**

`frontend/src/canvas/forms/NodeConfigForm.tsx`：`LlmSynthForm` 的 `return (<>...</>)` 改为用 `<Collapse defaultActiveKey={[]} items={[...]} />`（默认全折叠、可多开），把现有字段按组放入：

```tsx
  return (
    <>
      <NodeAssist nodeType="llm_synth" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Collapse defaultActiveKey={[]} items={[
        { key: 'model', label: '模型', children: (
          <Field label="模型">
            <Select style={{ width: '100%' }} value={config.model_config_id}
                    onChange={(v) => patch({ model_config_id: v })}
                    options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))} />
          </Field>
        ) },
        { key: 'prompt', label: '提示词', children: (
          <>
            <Field label="System Prompt">
              <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                              onChange={(e) => patch({ system_prompt: e.target.value })} />
            </Field>
            <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
              <Input.TextArea rows={6} value={config.user_prompt ?? ''}
                              onChange={(e) => patch({ user_prompt: e.target.value })} />
              <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
            </Field>
          </>
        ) },
        { key: 'advanced', label: '高级（输出 / 采样 / 参数）', children: (
          <>
            {/* 输出方式 + JSON/列名 + 扇出/并发/重试 + temperature/top_p/max_tokens/超时/JSON模式 + ThinkingControls
                —— 把原 LlmSynthForm 中这些 <Field>/<Space> 原样搬进此处 */}
          </>
        ) },
      ]} />
    </>
  )
```

把原 `LlmSynthForm` 中「输出方式」「JSON 输出列/输出列名」「扇出/并发/重试」「temperature…JSON模式」「ThinkingControls」全部移入 `advanced` 组的 children。文件顶部 antd import 加 `Collapse`。

> 注：节点路径（llm_synth/qc）的思考控件保留在「高级」组内可配（与 Agent 侧不同——Agent 侧才硬编码 xhigh）。

- [ ] **Step 2：QcForm / HttpFetchForm / AutoProcessForm / OutputNodeForm / InputNodeForm 折叠**

同法把各表单字段分组收进 `Collapse defaultActiveKey={[]}`：
- QcForm：`判定`（判定模型/pass_k）、`提示词`（system/user）、`回扫与反馈`（max_rounds/feedback_column）、`高级`（temperature…+ThinkingControls）。NodeAssist 仍在 Collapse 之外置顶。
- HttpFetchForm：`请求`（方法/URL/Body）、`鉴权与提取`（Headers/extract）、`高级`（并发/重试/超时）。
- AutoProcessForm：整体放入一个 `操作` 组（`defaultActiveKey={[]}`），或保持原样仅外套一层 Collapse；NodeAssist 不适用（auto_process 无助手）。
- OutputNodeForm / InputNodeForm：字段少，外套单个 Collapse 组即可（默认折叠）。

> 实现提示：每个表单的 `return` 用 `<Collapse defaultActiveKey={[]} items={[{key,label,children}, ...]} />`；children 直接放原有 `<Field>`/`<Space>` 子树，逻辑（patch/patchParams/onChange）完全不动，只是包裹位置变化。

- [ ] **Step 3：构建确认 + 手验**

Run: `cd "E:/代码/GraphFlow/frontend" && npm run build`
Expected: tsc 干净、构建成功。手验：打开各类型节点配置抽屉，默认看到一排折叠的分组标题，点开任意组展开/收起，可多组同时展开；所有字段编辑、列 I/O 三态、助手均正常。

- [ ] **Step 4：提交**

```bash
git -C "E:/代码/GraphFlow" add frontend/src/canvas/forms/NodeConfigForm.tsx
git -C "E:/代码/GraphFlow" commit -m "feat(ui): 节点配置面板折叠布局（全分组、默认全折叠、可多开）"
```

---

## 收尾（全部任务后）

- [ ] 跑全套后端：`cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`（应 ≥ 365 + 新增用例全绿）。
- [ ] 前端构建：`cd "E:/代码/GraphFlow/frontend" && npm run build` 干净。
- [ ] 用 superpowers:finishing-a-development-branch 收尾：本地 ff-merge 进 master、删除 batch20 分支。**不 push origin**（测试只本地，见 [[graphflow-tests-not-on-origin]]）。
- [ ] 写本批记忆（项目类）：两块特性 + 「pull 删测试已恢复修复」事件 + xhigh 单切口/日志 contextvar 双网关/前端 store 取舍。

## 自审备注（写计划时已核对）

- **Spec 覆盖**：Part1→Task1；Part2/3→Task5+Task6；Part4→Task2+3+4+Task7；Part5→Task8。compactor 走 llm.chat 的 xhigh/日志已分别在 Task1/Task3 覆盖。
- **类型一致**：`force_xhigh`、`log_context/log_model_call/current_ctx`、`LoggingModel`、`generate_node_config(... history=...) -> {reply,config}`、`useNodeAssist/setDraft/sendAssist`、`ModelCallLog` 字段、`NodeAssistReply/ModelLogEntry` 跨任务签名一致。
- **已知联动**：Task1 会再次改 batch19/Phase0 修过的思考断言（强制 xhigh）——属预期 TDD red→green；Task3 用 LoggingModel 包 create_model，`test_agent_factory` 经 WrapperModel 委托访问 settings/model_name 仍可用（勿加 isinstance 断言）。
