# GraphFlow P1（核心闭环）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 P1 核心闭环——dev 登录、模型配置、数据集上传/预览、画布编排（输入/LLM合成/自动处理/输出）、后台 DAG 执行（断点落盘、取消、失败行重跑）、进度轮询、结果导出。

**Architecture:** 单进程一体化：FastAPI 内嵌 asyncio 执行引擎 + SQLite(WAL)。引擎按**节点拓扑序**执行（行级节点节点内并发、逐行落盘断点；批级节点整表处理一次）。前端 React 18 + React Flow 画布，2 秒轮询进度。

**Tech Stack:** 后端 FastAPI / SQLAlchemy 2(async) / aiosqlite / openai SDK / pandas；前端 Vite / React 18 / TypeScript / @xyflow/react / Ant Design 5 / Zustand / react-router-dom。

**对设计文档的两处实现澄清**（KISS，功能等价）：
1. 引擎执行模型从"行完成即流向下游的流水线"简化为"节点拓扑序执行"：用户级信号量（默认 8）才是吞吐瓶颈，流水线化不提升总吞吐，只增加断点复杂度；且节点序模型让 P2 回扫（按轮次重过）更自然。行级语义保留为：节点内行级并发 + 每行独立落盘/重试。
2. P1 用 `create_all` 建表，Alembic 推迟到 P2 首次真实迁移时引入（以 P1 schema 为基线 stamp）。

**`run_rows` 落盘语义（全计划统一）：** 每条记录 = 节点的一个工作单元。行级节点（llm_synth）：每条输入行一条记录，`row_idx`=输入行序号，`data_json`=该行产出的输出行 JSON **数组**（扇出 N 条、正常 1 条）；批级节点（input/auto_process/output）：单条记录 `row_idx=0`，`data_json`=全部输出行数组。节点输出 = 按 `row_idx` 排序的 done 记录的 `data_json` 拼接。断点续跑 = 跳过 done 记录。

---

## 文件结构

```
GraphFlow/
├── .gitignore
├── README.md                          # Task 16
├── backend/
│   ├── pyproject.toml                 # uv 项目
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py                  # 环境变量配置（数据目录/密钥）
│   │   ├── db.py                      # async engine + WAL + session
│   │   ├── models.py                  # 全部 ORM 表
│   │   ├── auth.py                    # DevAuthProvider + cookie 会话 + 当前用户依赖
│   │   ├── crypto.py                  # api_key Fernet 加解密
│   │   ├── main.py                    # app 工厂、路由挂载、启动建表/恢复、静态托管
│   │   ├── routers/
│   │   │   ├── auth.py                # /api/auth/login /api/me
│   │   │   ├── model_configs.py       # 模型配置 CRUD + 连通性测试
│   │   │   ├── datasets.py            # 上传/列表/行预览/删除
│   │   │   ├── workflows.py           # 工作流 CRUD
│   │   │   └── runs.py                # 运行 创建/列表/详情/取消/重跑失败/行预览/导出
│   │   ├── services/
│   │   │   ├── file_parse.py          # JSONL/CSV/Excel/JSON → list[dict]
│   │   │   ├── llm.py                 # AsyncOpenAI 封装：重试+token用量
│   │   │   └── export.py              # rows → jsonl/csv/xlsx 文件
│   │   └── engine/
│   │       ├── graph.py               # 图解析/校验/拓扑序
│   │       ├── nodes.py               # auto_process 纯函数操作 + llm_synth 执行器
│   │       ├── runner.py              # 拓扑执行/断点/取消/进度落库
│   │       └── manager.py             # RunManager：提交/取消/用户信号量/启动恢复
│   └── tests/
│       ├── conftest.py
│       ├── test_config.py
│       ├── test_db_models.py
│       ├── test_auth.py
│       ├── test_model_configs.py
│       ├── test_file_parse.py
│       ├── test_datasets.py
│       ├── test_graph.py
│       ├── test_workflows.py
│       ├── test_llm.py
│       ├── test_auto_process.py
│       ├── test_runner.py
│       ├── test_llm_synth.py
│       └── test_runs_api.py
└── frontend/
    ├── package.json / vite.config.ts / tsconfig.json / index.html
    └── src/
        ├── main.tsx / App.tsx         # 路由 + antd 布局
        ├── api/client.ts              # fetch 封装 + 各资源 API
        ├── api/types.ts               # 与后端对齐的 TS 类型
        ├── stores/auth.ts             # zustand 登录态
        ├── pages/
        │   ├── LoginPage.tsx
        │   ├── ModelsPage.tsx
        │   ├── DatasetsPage.tsx
        │   ├── WorkflowsPage.tsx
        │   ├── CanvasPage.tsx         # React Flow 画布 + 配置抽屉
        │   └── RunDetailPage.tsx      # 轮询进度/失败行/导出
        └── canvas/
            ├── nodeTypes.tsx          # 自定义节点渲染
            ├── serialize.ts           # ReactFlow 状态 ↔ graph_json
            └── forms/                 # 四种节点的配置表单
```

约定：后端命令在 `backend/` 下执行 `uv run pytest ...`；前端命令在 `frontend/` 下执行。所有提交信息用中文。

---

### Task 1: 仓库脚手架 + backend uv 项目 + 配置模块

**Files:**
- Create: `.gitignore`
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`（空文件）
- Create: `backend/app/config.py`
- Test: `backend/tests/test_config.py`

- [ ] **Step 1: 写 .gitignore 与 pyproject.toml**

`.gitignore`：

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
data/
node_modules/
dist/
```

`backend/pyproject.toml`：

```toml
[project]
name = "graphflow-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi[standard]>=0.115",
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "openai>=1.60",
    "pandas>=2.2",
    "openpyxl>=3.1",
    "cryptography>=43",
    "itsdangerous>=2.2",
    "pydantic-settings>=2.6",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.25", "httpx>=0.28"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 安装依赖**

Run: `cd backend; uv sync`
Expected: 创建 `.venv`，依赖安装成功（fastapi[standard] 自带 uvicorn 与 python-multipart）。

- [ ] **Step 3: 写失败测试 `backend/tests/test_config.py`**

```python
from pathlib import Path


def test_default_settings():
    from app.config import Settings
    s = Settings()
    assert s.data_dir == Path("data")
    assert s.db_url.startswith("sqlite+aiosqlite:///")


def test_env_override(monkeypatch):
    monkeypatch.setenv("GRAPHFLOW_DATA_DIR", "/tmp/gf")
    monkeypatch.setenv("GRAPHFLOW_SECRET_KEY", "s3cret")
    from app.config import Settings
    s = Settings()
    assert s.data_dir == Path("/tmp/gf")
    assert s.secret_key == "s3cret"
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: app.config`）

- [ ] **Step 5: 实现 `backend/app/config.py`（及空 `app/__init__.py`）**

```python
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'graphflow.db'}"


settings = Settings()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 7: 提交**

```bash
git add .gitignore backend/
git commit -m "feat: backend 脚手架与配置模块"
```

---

### Task 2: 数据库层（全部 ORM 表 + WAL + 启动建表）

**Files:**
- Create: `backend/app/db.py`
- Create: `backend/app/models.py`
- Test: `backend/tests/test_db_models.py`
- Test: `backend/tests/conftest.py`

- [ ] **Step 1: 写共享 fixture `backend/tests/conftest.py`**

```python
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base


@pytest.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 2: 写失败测试 `backend/tests/test_db_models.py`**

```python
from sqlalchemy import select

from app.models import Dataset, ModelConfig, Run, RunNodeState, RunRow, User, Workflow, WorkflowVersion


async def test_create_user_and_query(session_factory):
    async with session_factory() as s:
        s.add(User(username="alice", display_name="Alice"))
        await s.commit()
    async with session_factory() as s:
        u = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
        assert u.max_llm_concurrency == 8
        assert u.auth_provider == "dev"


async def test_run_row_unique_unit(session_factory):
    async with session_factory() as s:
        u = User(username="bob")
        s.add(u)
        await s.flush()
        wf = Workflow(user_id=u.id, name="wf", graph_json="{}")
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}")
        s.add(ver)
        await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run)
        await s.flush()
        s.add(RunRow(run_id=run.id, node_id="n1", row_idx=0, status="done", data_json="[]"))
        s.add(RunNodeState(run_id=run.id, node_id="n1", status="done", total=1, done=1))
        await s.commit()
        assert run.status == "queued"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_db_models.py -v`
Expected: FAIL（`ModuleNotFoundError: app.models`）

- [ ] **Step 4: 实现 `backend/app/models.py`**

```python
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str] = mapped_column(default="")
    auth_provider: Mapped[str] = mapped_column(default="dev")
    max_llm_concurrency: Mapped[int] = mapped_column(default=8)
    created_at: Mapped[datetime] = mapped_column(default=now)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    model_name: Mapped[str] = mapped_column(default="")  # 实际请求用的模型 ID，如 qwen-max
    base_url: Mapped[str]
    api_key_enc: Mapped[str]
    default_params_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=now)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    source: Mapped[str] = mapped_column(default="upload")  # upload / run
    original_filename: Mapped[str] = mapped_column(default="")
    file_path: Mapped[str] = mapped_column(default="")
    row_count: Mapped[int] = mapped_column(default=0)
    columns_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=now)


class DatasetRow(Base):
    __tablename__ = "dataset_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    idx: Mapped[int]
    data_json: Mapped[str] = mapped_column(Text)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    graph_json: Mapped[str] = mapped_column(Text, default='{"nodes": [], "edges": []}')
    is_template: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    version: Mapped[int]
    graph_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    workflow_version_id: Mapped[int] = mapped_column(ForeignKey("workflow_versions.id"))
    status: Mapped[str] = mapped_column(default="queued")  # queued/running/cancelled/completed/failed
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class RunNodeState(Base):
    __tablename__ = "run_node_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending/running/done/failed
    total: Mapped[int] = mapped_column(default=0)
    done: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    __table_args__ = (Index("ix_node_state_unit", "run_id", "node_id", unique=True),)


class RunRow(Base):
    __tablename__ = "run_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str]
    row_idx: Mapped[int]
    attempt: Mapped[int] = mapped_column(default=0)
    qc_round: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="pending")  # pending/running/done/failed
    data_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    __table_args__ = (Index("ix_run_row_unit", "run_id", "node_id", "row_idx", unique=True),)
```

- [ ] **Step 5: 实现 `backend/app/db.py`（WAL + session 工厂 + 建表）**

```python
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

engine: AsyncEngine | None = None
session_factory: async_sessionmaker | None = None


def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


async def init_db() -> None:
    global engine, session_factory
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.db_url)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker:
    assert session_factory is not None, "init_db() 未调用"
    return session_factory
```

- [ ] **Step 6: 运行全部测试确认通过**

Run: `uv run pytest -v`
Expected: 4 passed（test_config 2 + test_db_models 2）

- [ ] **Step 7: 提交**

```bash
git add backend/
git commit -m "feat: 数据库层——全部 ORM 表与 WAL 初始化"
```

---

### Task 3: 认证（DevAuthProvider + Cookie 会话 + app 工厂）

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/app/routers/__init__.py`（空）
- Create: `backend/app/routers/auth.py`
- Create: `backend/app/main.py`
- Modify: `backend/app/db.py`（追加 `get_session` 依赖）
- Modify: `backend/tests/conftest.py`（追加 client fixtures）
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: 在 conftest.py 追加 API 测试 fixtures**

```python
import httpx

from app.config import settings


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app import db
    await db.init_db()
    from app.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.engine.dispose()


@pytest.fixture
async def auth_client(client):
    await client.post("/api/auth/login", json={"username": "tester"})
    return client
```

注意：`httpx.ASGITransport` 不触发 lifespan，所以 fixture 自行调用 `init_db()`；生产由 lifespan 调用。

- [ ] **Step 2: 写失败测试 `backend/tests/test_auth.py`**

```python
async def test_me_unauthorized(client):
    r = await client.get("/api/me")
    assert r.status_code == 401


async def test_login_then_me(client):
    r = await client.post("/api/auth/login", json={"username": "alice"})
    assert r.status_code == 200
    assert "gf_session" in r.cookies
    me = await client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_login_idempotent(client):
    a = (await client.post("/api/auth/login", json={"username": "alice"})).json()
    b = (await client.post("/api/auth/login", json={"username": "alice"})).json()
    assert a["id"] == b["id"]


async def test_login_rejects_blank(client):
    r = await client.post("/api/auth/login", json={"username": "  "})
    assert r.status_code == 422
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL（`ModuleNotFoundError: app.main`）

- [ ] **Step 4: 在 `backend/app/db.py` 末尾追加 session 依赖**

```python
async def get_session():
    async with get_session_factory()() as session:
        yield session
```

- [ ] **Step 5: 实现 `backend/app/auth.py`**

```python
from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User

COOKIE_NAME = "gf_session"
COOKIE_MAX_AGE = 7 * 24 * 3600


def make_session_cookie(user_id: int) -> str:
    return TimestampSigner(settings.secret_key).sign(str(user_id)).decode()


def parse_session_cookie(value: str) -> int | None:
    try:
        raw = TimestampSigner(settings.secret_key).unsign(value, max_age=COOKIE_MAX_AGE)
        return int(raw)
    except (BadSignature, ValueError):
        return None


class DevAuthProvider:
    """开发模式：输入用户名即登录，不存在则自动建用户。SSO 协议确认后新增同接口实现。"""

    async def login(self, session: AsyncSession, username: str) -> User:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, display_name=username, auth_provider="dev")
            session.add(user)
            await session.commit()
        return user


auth_provider = DevAuthProvider()


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user
```

- [ ] **Step 6: 实现 `backend/app/routers/auth.py`**

```python
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import COOKIE_MAX_AGE, COOKIE_NAME, auth_provider, get_current_user, make_session_cookie
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str


def _user_out(user: User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


@router.post("/auth/login")
async def login(body: LoginIn, response: Response, session: AsyncSession = Depends(get_session)):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    user = await auth_provider.login(session, username)
    response.set_cookie(COOKIE_NAME, make_session_cookie(user.id), httponly=True, max_age=COOKIE_MAX_AGE)
    return _user_out(user)


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return _user_out(user)
```

- [ ] **Step 7: 实现 `backend/app/main.py`**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.routers import auth


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GraphFlow", lifespan=lifespan)
    app.include_router(auth.router)
    return app


app = create_app()
```

- [ ] **Step 8: 运行测试确认通过**

Run: `uv run pytest tests/test_auth.py -v`
Expected: 4 passed

- [ ] **Step 9: 提交**

```bash
git add backend/
git commit -m "feat: dev 模式认证与 cookie 会话"
```

---

### Task 4: api_key 加密 + 模型配置 CRUD

**Files:**
- Create: `backend/app/crypto.py`
- Create: `backend/app/routers/model_configs.py`
- Modify: `backend/app/main.py`（挂载路由）
- Test: `backend/tests/test_model_configs.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_model_configs.py`**

```python
from sqlalchemy import select

from app import crypto
from app.models import ModelConfig

PAYLOAD = {
    "name": "内网Qwen",
    "model_name": "qwen-max",
    "base_url": "http://10.0.0.1:8000/v1",
    "api_key": "sk-secret-123",
    "default_params": {"temperature": 0.7},
}


def test_crypto_roundtrip():
    token = crypto.encrypt("sk-abc")
    assert token != "sk-abc"
    assert crypto.decrypt(token) == "sk-abc"


async def test_create_and_list_masks_key(auth_client):
    r = await auth_client.post("/api/models", json=PAYLOAD)
    assert r.status_code == 200
    listed = (await auth_client.get("/api/models")).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "内网Qwen"
    assert listed[0]["api_key_set"] is True
    assert "sk-secret-123" not in str(listed[0])


async def test_update_without_key_keeps_old(auth_client, session_factory):
    mid = (await auth_client.post("/api/models", json=PAYLOAD)).json()["id"]
    r = await auth_client.put(f"/api/models/{mid}", json={**PAYLOAD, "api_key": "", "name": "改名"})
    assert r.status_code == 200
    async with session_factory() as s:
        mc = (await s.execute(select(ModelConfig).where(ModelConfig.id == mid))).scalar_one()
        assert crypto.decrypt(mc.api_key_enc) == "sk-secret-123"
        assert mc.name == "改名"


async def test_delete(auth_client):
    mid = (await auth_client.post("/api/models", json=PAYLOAD)).json()["id"]
    assert (await auth_client.delete(f"/api/models/{mid}")).status_code == 200
    assert (await auth_client.get("/api/models")).json() == []


async def test_user_isolation(auth_client):
    await auth_client.post("/api/models", json=PAYLOAD)
    await auth_client.post("/api/auth/login", json={"username": "other"})  # 切换用户
    assert (await auth_client.get("/api/models")).json() == []
```

注意：`test_update_without_key_keeps_old` 需要 API 与 fixture 共用同一个库——`client` fixture 已把 `settings.data_dir` 指到 tmp_path，因此这里的 `session_factory` fixture 改为复用 `app.db` 的工厂。把 conftest.py 中 `session_factory` fixture 替换为：

```python
@pytest.fixture
async def session_factory(client):
    from app import db
    return db.get_session_factory()
```

并删除原 `engine` fixture，`test_db_models.py` 改用此 fixture（自动获得建好的库）。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_model_configs.py -v`
Expected: FAIL（`ModuleNotFoundError: app.crypto`）

- [ ] **Step 3: 实现 `backend/app/crypto.py`**

```python
import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
```

- [ ] **Step 4: 实现 `backend/app/routers/model_configs.py`**

```python
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.auth import get_current_user
from app.db import get_session
from app.models import ModelConfig, User

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelConfigIn(BaseModel):
    name: str
    model_name: str
    base_url: str
    api_key: str = ""
    default_params: dict = {}


def _out(mc: ModelConfig) -> dict:
    return {
        "id": mc.id,
        "name": mc.name,
        "model_name": mc.model_name,
        "base_url": mc.base_url,
        "api_key_set": bool(mc.api_key_enc),
        "default_params": json.loads(mc.default_params_json),
    }


async def _get_owned(mc_id: int, user: User, session: AsyncSession) -> ModelConfig:
    mc = await session.get(ModelConfig, mc_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return mc


@router.get("")
async def list_models(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ModelConfig).where(ModelConfig.user_id == user.id).order_by(ModelConfig.id)
    )).scalars().all()
    return [_out(m) for m in rows]


@router.post("")
async def create_model(body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = ModelConfig(
        user_id=user.id, name=body.name, model_name=body.model_name, base_url=body.base_url,
        api_key_enc=crypto.encrypt(body.api_key) if body.api_key else "",
        default_params_json=json.dumps(body.default_params, ensure_ascii=False),
    )
    session.add(mc)
    await session.commit()
    return _out(mc)


@router.put("/{mc_id}")
async def update_model(mc_id: int, body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    mc.name, mc.model_name, mc.base_url = body.name, body.model_name, body.base_url
    mc.default_params_json = json.dumps(body.default_params, ensure_ascii=False)
    if body.api_key:  # 留空表示不修改 key
        mc.api_key_enc = crypto.encrypt(body.api_key)
    await session.commit()
    return _out(mc)


@router.delete("/{mc_id}")
async def delete_model(mc_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    await session.delete(mc)
    await session.commit()
    return {"ok": True}
```

- [ ] **Step 5: 在 `backend/app/main.py` 挂载路由**

```python
from app.routers import auth, model_configs
# create_app() 内：
    app.include_router(model_configs.router)
```

- [ ] **Step 6: 按 Step 1 注意事项调整 conftest 并运行全部测试**

Run: `uv run pytest -v`
Expected: 全部通过（含 test_db_models 改用新 fixture 后）

- [ ] **Step 7: 提交**

```bash
git add backend/
git commit -m "feat: api_key 加密与模型配置 CRUD"
```

---

### Task 5: 文件解析服务（JSONL/CSV/Excel/JSON → 行式记录）

**Files:**
- Create: `backend/app/services/__init__.py`（空）
- Create: `backend/app/services/file_parse.py`
- Test: `backend/tests/test_file_parse.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_file_parse.py`**

```python
import io

import pandas as pd
import pytest

from app.services.file_parse import parse_file, union_columns


def test_jsonl():
    content = '{"q": "你好", "a": "world"}\n\n{"q": "第二行"}\n'.encode("utf-8")
    rows = parse_file("a.jsonl", content)
    assert rows == [{"q": "你好", "a": "world"}, {"q": "第二行"}]


def test_json_array_and_single():
    assert parse_file("a.json", b'[{"x": 1}, {"x": 2}]') == [{"x": 1}, {"x": 2}]
    assert parse_file("a.json", '{"x": "单条"}'.encode()) == [{"x": "单条"}]


def test_csv():
    rows = parse_file("a.csv", "q,a\n你好,world\n".encode("utf-8"))
    assert rows == [{"q": "你好", "a": "world"}]


def test_xlsx():
    buf = io.BytesIO()
    pd.DataFrame([{"q": "你好", "a": 1}]).to_excel(buf, index=False)
    rows = parse_file("a.xlsx", buf.getvalue())
    assert rows == [{"q": "你好", "a": 1}]


def test_unsupported_suffix():
    with pytest.raises(ValueError, match="不支持"):
        parse_file("a.txt", b"hello")


def test_union_columns_keeps_order():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert union_columns(rows) == ["a", "b", "c"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_file_parse.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.file_parse`）

- [ ] **Step 3: 实现 `backend/app/services/file_parse.py`**

```python
import io
import json
from pathlib import Path

import pandas as pd


def parse_file(filename: str, content: bytes) -> list[dict]:
    """把上传文件解析为行式记录。不支持的格式抛 ValueError。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".jsonl":
        return [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(content)
        return data if isinstance(data, list) else [data]
    if suffix == ".csv":
        df = pd.read_csv(io.BytesIO(content))
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(content))
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict(orient="records")


def union_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_file_parse.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: 上传文件解析服务"
```

---

### Task 6: 数据集 API（多文件上传/列表/行预览/删除）

**Files:**
- Create: `backend/app/routers/datasets.py`
- Modify: `backend/app/main.py`（挂载路由）
- Test: `backend/tests/test_datasets.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_datasets.py`**

```python
JSONL = '{"q": "你好"}\n{"q": "第二"}\n{"q": "第三"}\n'.encode("utf-8")


async def upload(client, *files):
    payload = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return await client.post("/api/datasets/upload", files=payload)


async def test_upload_single(auth_client):
    r = await upload(auth_client, ("种子.jsonl", JSONL))
    assert r.status_code == 200
    ds = r.json()[0]
    assert ds["name"] == "种子"
    assert ds["row_count"] == 3
    assert ds["columns"] == ["q"]


async def test_upload_multiple_files(auth_client):
    r = await upload(auth_client, ("a.jsonl", JSONL), ("b.csv", "q\nx\n".encode()))
    assert [d["row_count"] for d in r.json()] == [3, 1]


async def test_upload_bad_file_422(auth_client):
    r = await upload(auth_client, ("bad.txt", b"hello"))
    assert r.status_code == 422
    assert "bad.txt" in r.json()["detail"]


async def test_rows_pagination(auth_client):
    ds = (await upload(auth_client, ("a.jsonl", JSONL))).json()[0]
    r = (await auth_client.get(f"/api/datasets/{ds['id']}/rows?page=2&page_size=2")).json()
    assert r["total"] == 3
    assert r["rows"] == [{"q": "第三"}]


async def test_delete(auth_client):
    ds = (await upload(auth_client, ("a.jsonl", JSONL))).json()[0]
    assert (await auth_client.delete(f"/api/datasets/{ds['id']}")).status_code == 200
    assert (await auth_client.get("/api/datasets")).json() == []


async def test_user_isolation(auth_client):
    await upload(auth_client, ("a.jsonl", JSONL))
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/datasets")).json() == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_datasets.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现 `backend/app/routers/datasets.py`**

```python
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import delete as sa_delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.models import Dataset, DatasetRow, User
from app.services.file_parse import parse_file, union_columns

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


def _out(ds: Dataset) -> dict:
    return {
        "id": ds.id, "name": ds.name, "source": ds.source,
        "original_filename": ds.original_filename, "row_count": ds.row_count,
        "columns": json.loads(ds.columns_json), "created_at": ds.created_at.isoformat(),
    }


async def _get_owned(ds_id: int, user: User, session: AsyncSession) -> Dataset:
    ds = await session.get(Dataset, ds_id)
    if ds is None or ds.user_id != user.id:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return ds


async def create_dataset(session: AsyncSession, user_id: int, name: str, rows: list[dict],
                         source: str = "upload", original_filename: str = "") -> Dataset:
    """供上传与（后续）运行结果保存共用。"""
    ds = Dataset(user_id=user_id, name=name, source=source, original_filename=original_filename,
                 row_count=len(rows), columns_json=json.dumps(union_columns(rows), ensure_ascii=False))
    session.add(ds)
    await session.flush()
    if rows:
        await session.execute(insert(DatasetRow), [
            {"dataset_id": ds.id, "idx": i, "data_json": json.dumps(r, ensure_ascii=False)}
            for i, r in enumerate(rows)
        ])
    await session.commit()
    return ds


@router.post("/upload")
async def upload(files: list[UploadFile], user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    results = []
    for f in files:
        content = await f.read()
        try:
            rows = parse_file(f.filename, content)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"{f.filename} 解析失败: {e}")
        ds = await create_dataset(session, user.id, Path(f.filename).stem, rows,
                                  original_filename=f.filename)
        upload_dir = settings.data_dir / "uploads" / str(user.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{ds.id}_{f.filename}"
        file_path.write_bytes(content)
        ds.file_path = str(file_path)
        await session.commit()
        results.append(_out(ds))
    return results


@router.get("")
async def list_datasets(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.id.desc())
    )).scalars().all()
    return [_out(d) for d in rows]


@router.get("/{ds_id}/rows")
async def dataset_rows(ds_id: int, page: int = 1, page_size: int = 20,
                       user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    stmt = (select(DatasetRow).where(DatasetRow.dataset_id == ds.id)
            .order_by(DatasetRow.idx).offset((page - 1) * page_size).limit(page_size))
    rows = (await session.execute(stmt)).scalars().all()
    return {"total": ds.row_count, "rows": [json.loads(r.data_json) for r in rows]}


@router.delete("/{ds_id}")
async def delete_dataset(ds_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
    if ds.file_path:
        Path(ds.file_path).unlink(missing_ok=True)
    await session.delete(ds)
    await session.commit()
    return {"ok": True}
```

- [ ] **Step 4: 挂载路由（main.py）并运行测试**

`create_app()` 内追加 `app.include_router(datasets.router)`（import 同步更新）。

Run: `uv run pytest tests/test_datasets.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: 数据集上传/预览/删除 API"
```

---

### Task 7: 图结构（解析/校验/拓扑序）+ 工作流 CRUD

**Files:**
- Create: `backend/app/engine/__init__.py`（空）
- Create: `backend/app/engine/graph.py`
- Create: `backend/app/routers/workflows.py`
- Modify: `backend/app/main.py`（挂载路由）
- Test: `backend/tests/test_graph.py`
- Test: `backend/tests/test_workflows.py`

设计决定：**保存工作流不做图校验**（画布允许保存半成品），完整校验在创建运行时执行（Task 12）。

- [ ] **Step 1: 写失败测试 `backend/tests/test_graph.py`**

```python
import pytest

from app.engine.graph import GraphError, parse_graph, topo_order, upstream_ids, validate_graph


def g(nodes, edges):
    return parse_graph({
        "nodes": [{"id": i, "type": t, "config": {}} for i, t in nodes],
        "edges": [{"source": s, "target": t, "kind": k} for s, t, k in edges],
    })


LINEAR = [("a", "input"), ("b", "llm_synth"), ("c", "output")]


def test_topo_linear():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal")])
    assert [n.id for n in topo_order(graph)] == ["a", "b", "c"]


def test_topo_dag_branch_merge():
    nodes = LINEAR + [("d", "auto_process")]
    edges = [("a", "b", "normal"), ("a", "d", "normal"), ("b", "c", "normal"), ("d", "c", "normal")]
    order = [n.id for n in topo_order(g(nodes, edges))]
    assert order.index("a") < order.index("b") < order.index("c")
    assert order.index("a") < order.index("d") < order.index("c")


def test_cycle_rejected():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal"), ("c", "a", "normal")])
    with pytest.raises(GraphError, match="环"):
        topo_order(graph)


def test_validate_unknown_type():
    with pytest.raises(GraphError, match="未知节点类型"):
        validate_graph(g([("a", "magic")], []))


def test_validate_dangling_edge():
    with pytest.raises(GraphError, match="不存在的节点"):
        validate_graph(g(LINEAR, [("a", "nope", "normal")]))


def test_validate_duplicate_id():
    with pytest.raises(GraphError, match="重复"):
        validate_graph(g([("a", "input"), ("a", "output")], []))


def test_upstream_ids():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal")])
    assert upstream_ids(graph, "c") == ["b"]
    assert upstream_ids(graph, "a") == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_graph.py -v`
Expected: FAIL（`ModuleNotFoundError: app.engine.graph`）

- [ ] **Step 3: 实现 `backend/app/engine/graph.py`**

```python
import json
from dataclasses import dataclass

NODE_TYPES = {"input", "llm_synth", "auto_process", "output"}  # P2 增加 qc


class GraphError(ValueError):
    pass


@dataclass
class Node:
    id: str
    type: str
    config: dict


@dataclass
class Graph:
    nodes: list[Node]
    edges: list[dict]  # {"source", "target", "kind": "normal"|"rescan"}


def parse_graph(graph_json: str | dict) -> Graph:
    data = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
    nodes = [Node(id=n["id"], type=n["type"], config=n.get("config", {})) for n in data.get("nodes", [])]
    edges = [{"source": e["source"], "target": e["target"], "kind": e.get("kind", "normal")}
             for e in data.get("edges", [])]
    return Graph(nodes=nodes, edges=edges)


def validate_graph(g: Graph) -> None:
    ids = [n.id for n in g.nodes]
    if len(ids) != len(set(ids)):
        raise GraphError("节点 id 重复")
    id_set = set(ids)
    for n in g.nodes:
        if n.type not in NODE_TYPES:
            raise GraphError(f"未知节点类型: {n.type}")
    for e in g.edges:
        if e["source"] not in id_set or e["target"] not in id_set:
            raise GraphError("边指向不存在的节点")
    topo_order(g)


def topo_order(g: Graph) -> list[Node]:
    """Kahn 算法，仅按 normal 边；有环抛 GraphError。"""
    normal = [e for e in g.edges if e["kind"] == "normal"]
    by_id = {n.id: n for n in g.nodes}
    indeg = {n.id: 0 for n in g.nodes}
    for e in normal:
        indeg[e["target"]] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(by_id[nid])
        for e in normal:
            if e["source"] == nid:
                indeg[e["target"]] -= 1
                if indeg[e["target"]] == 0:
                    queue.append(e["target"])
    if len(order) != len(g.nodes):
        raise GraphError("工作流包含环（普通边必须无环）")
    return order


def upstream_ids(g: Graph, node_id: str) -> list[str]:
    return [e["source"] for e in g.edges if e["target"] == node_id and e["kind"] == "normal"]
```

- [ ] **Step 4: 写失败测试 `backend/tests/test_workflows.py`**

```python
GRAPH = {"nodes": [{"id": "n1", "type": "input", "config": {}}], "edges": []}


async def test_crud(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "测试流"})).json()
    assert wf["name"] == "测试流"
    r = await auth_client.put(f"/api/workflows/{wf['id']}", json={"name": "改名", "graph": GRAPH})
    assert r.status_code == 200
    got = (await auth_client.get(f"/api/workflows/{wf['id']}")).json()
    assert got["name"] == "改名"
    assert got["graph"]["nodes"][0]["id"] == "n1"
    assert len((await auth_client.get("/api/workflows")).json()) == 1
    await auth_client.delete(f"/api/workflows/{wf['id']}")
    assert (await auth_client.get("/api/workflows")).json() == []


async def test_save_incomplete_graph_allowed(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "半成品"})).json()
    bad = {"nodes": [{"id": "x", "type": "llm_synth", "config": {}}], "edges": []}
    assert (await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": bad})).status_code == 200


async def test_user_isolation(auth_client):
    await auth_client.post("/api/workflows", json={"name": "我的"})
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/workflows")).json() == []
```

- [ ] **Step 5: 实现 `backend/app/routers/workflows.py`**

```python
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import User, Workflow

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    name: str


class WorkflowUpdate(BaseModel):
    name: str | None = None
    graph: dict | None = None


def _out(wf: Workflow) -> dict:
    return {"id": wf.id, "name": wf.name, "graph": json.loads(wf.graph_json),
            "updated_at": wf.updated_at.isoformat()}


async def get_owned_workflow(wf_id: int, user: User, session: AsyncSession) -> Workflow:
    wf = await session.get(Workflow, wf_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return wf


@router.get("")
async def list_workflows(user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Workflow).where(Workflow.user_id == user.id).order_by(Workflow.updated_at.desc())
    )).scalars().all()
    return [{"id": w.id, "name": w.name, "updated_at": w.updated_at.isoformat()} for w in rows]


@router.post("")
async def create_workflow(body: WorkflowCreate, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = Workflow(user_id=user.id, name=body.name)
    session.add(wf)
    await session.commit()
    return _out(wf)


@router.get("/{wf_id}")
async def get_workflow(wf_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    return _out(await get_owned_workflow(wf_id, user, session))


@router.put("/{wf_id}")
async def update_workflow(wf_id: int, body: WorkflowUpdate, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    if body.name is not None:
        wf.name = body.name
    if body.graph is not None:
        wf.graph_json = json.dumps(body.graph, ensure_ascii=False)
    await session.commit()
    return _out(wf)


@router.delete("/{wf_id}")
async def delete_workflow(wf_id: int, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    await session.delete(wf)
    await session.commit()
    return {"ok": True}
```

- [ ] **Step 6: 挂载路由并运行全部测试**

`create_app()` 内追加 `app.include_router(workflows.router)`。

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add backend/
git commit -m "feat: 图结构校验与工作流 CRUD"
```

---

### Task 8: LLM 客户端（重试 + token 用量）+ 模型连通性测试端点

**Files:**
- Create: `backend/app/services/llm.py`
- Modify: `backend/app/routers/model_configs.py`（追加 /test 端点）
- Test: `backend/tests/test_llm.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_llm.py`**

```python
from types import SimpleNamespace

import pytest

from app.models import ModelConfig
from app.services import llm


def fake_response(text="好的"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeClient:
    """behavior(call_no, kwargs) -> response 或抛异常"""

    def __init__(self, behavior):
        self.calls = 0
        outer = self

        async def create(**kwargs):
            outer.calls += 1
            outer.last_kwargs = kwargs
            return behavior(outer.calls, kwargs)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen-max",
                       base_url="http://x/v1", api_key_enc=crypto.encrypt("sk-1"),
                       default_params_json='{"temperature": 0.5}')


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)


async def test_chat_success(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response("你好"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    text, usage = await llm.chat(mc(), "系统", "用户")
    assert text == "你好"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert fake.last_kwargs["model"] == "qwen-max"
    assert fake.last_kwargs["temperature"] == 0.5  # default_params 生效
    assert fake.last_kwargs["messages"][0] == {"role": "system", "content": "系统"}


async def test_params_override_and_json_mode(monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response())
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    await llm.chat(mc(), "", "u", params={"temperature": 0.9, "json_mode": True, "max_tokens": 100})
    assert fake.last_kwargs["temperature"] == 0.9
    assert fake.last_kwargs["max_tokens"] == 100
    assert fake.last_kwargs["response_format"] == {"type": "json_object"}
    assert fake.last_kwargs["messages"][0]["role"] == "user"  # 空 system 不发送


async def test_retry_then_success(monkeypatch):
    def behavior(n, kw):
        if n == 1:
            raise RuntimeError("boom")
        return fake_response()

    fake = FakeClient(behavior)
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    text, _ = await llm.chat(mc(), "", "u", retries=3)
    assert fake.calls == 2


async def test_retries_exhausted(monkeypatch):
    def behavior(n, kw):
        raise RuntimeError("always")

    fake = FakeClient(behavior)
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    with pytest.raises(llm.LLMError, match="always"):
        await llm.chat(mc(), "", "u", retries=2)
    assert fake.calls == 2


async def test_model_test_endpoint(auth_client, monkeypatch):
    fake = FakeClient(lambda n, kw: fake_response("pong"))
    monkeypatch.setattr(llm, "_client", lambda _: fake)
    payload = {"name": "m", "model_name": "qwen-max", "base_url": "http://x/v1",
               "api_key": "sk-1", "default_params": {}}
    mid = (await auth_client.post("/api/models", json=payload)).json()["id"]
    r = (await auth_client.post(f"/api/models/{mid}/test")).json()
    assert r == {"ok": True, "reply": "pong"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.llm`）

- [ ] **Step 3: 实现 `backend/app/services/llm.py`**

```python
import asyncio
import json

from openai import AsyncOpenAI

from app import crypto
from app.models import ModelConfig

BACKOFF_BASE = 1  # 秒；重试等待 BACKOFF_BASE * 2**attempt，测试中置 0


class LLMError(Exception):
    pass


def _client(mc: ModelConfig) -> AsyncOpenAI:
    api_key = crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none"
    return AsyncOpenAI(base_url=mc.base_url, api_key=api_key)


async def chat(mc: ModelConfig, system_prompt: str, user_prompt: str,
               params: dict | None = None, retries: int = 3) -> tuple[str, dict]:
    """单次对话调用。返回 (文本, usage)。重试耗尽抛 LLMError。"""
    merged = {**json.loads(mc.default_params_json), **(params or {})}
    kwargs: dict = {}
    for key in ("temperature", "top_p", "max_tokens"):
        if merged.get(key) is not None:
            kwargs[key] = merged[key]
    if merged.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    client = _client(mc)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=mc.model_name, messages=messages,
                timeout=merged.get("timeout", 120), **kwargs)
            usage = {"prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                     "completion_tokens": resp.usage.completion_tokens if resp.usage else 0}
            return resp.choices[0].message.content or "", usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(BACKOFF_BASE * 2 ** attempt)
    raise LLMError(str(last_err))
```

- [ ] **Step 4: 在 `backend/app/routers/model_configs.py` 追加 /test 端点**

```python
from app.services import llm

@router.post("/{mc_id}/test")
async def test_model(mc_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    try:
        text, _ = await llm.chat(mc, "", "ping", params={"max_tokens": 8}, retries=1)
        return {"ok": True, "reply": text[:100]}
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_llm.py -v`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: LLM 客户端（重试/用量）与连通性测试"
```

---

### Task 9: 自动处理节点操作（纯函数）

**Files:**
- Create: `backend/app/engine/nodes.py`
- Test: `backend/tests/test_auto_process.py`

操作配置统一为 `{"op": <名称>, ...参数}`，节点 config 为 `{"operations": [op, ...], "seed": 可选}`，顺序执行。脏数据导致的 cast 失败直接抛 ValueError——批级节点整体失败并展示错误，用户修数据后重跑。

- [ ] **Step 1: 写失败测试 `backend/tests/test_auto_process.py`**

```python
import pytest

from app.engine.nodes import apply_operations

ROWS = [{"q": "你好", "n": "1"}, {"q": "你好", "n": "2"}, {"q": "world", "n": "3"}]


def test_dedup_by_columns():
    out = apply_operations(ROWS, [{"op": "dedup", "columns": ["q"]}])
    assert [r["n"] for r in out] == ["1", "3"]  # 保留首次出现


def test_dedup_all_columns_default():
    rows = [{"a": 1}, {"a": 1}, {"a": 2}]
    assert apply_operations(rows, [{"op": "dedup"}]) == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize("mode,value,expected_n", [
    ("min_len", 3, ["3"]),          # len("world")=5 >= 3
    ("max_len", 2, ["1", "2"]),     # len("你好")=2
    ("contains", "world", ["3"]),
    ("not_contains", "world", ["1", "2"]),
    ("regex", "^你", ["1", "2"]),
])
def test_filter_modes(mode, value, expected_n):
    out = apply_operations(ROWS, [{"op": "filter", "column": "q", "mode": mode, "value": value}])
    assert [r["n"] for r in out] == expected_n


def test_rename_drop_concat():
    out = apply_operations(ROWS[:1], [
        {"op": "rename", "mapping": {"q": "question"}},
        {"op": "concat", "target": "merged", "columns": ["question", "n"], "sep": "-"},
        {"op": "drop", "columns": ["n"]},
    ])
    assert out == [{"question": "你好", "merged": "你好-1"}]


def test_cast():
    out = apply_operations([{"x": "3"}], [{"op": "cast", "column": "x", "to": "int"}])
    assert out == [{"x": 3}]
    with pytest.raises(ValueError):
        apply_operations([{"x": "abc"}], [{"op": "cast", "column": "x", "to": "int"}])


def test_sample_and_shuffle_deterministic_with_seed():
    rows = [{"i": i} for i in range(10)]
    a = apply_operations(rows, [{"op": "sample", "n": 5}], seed=42)
    b = apply_operations(rows, [{"op": "sample", "n": 5}], seed=42)
    assert a == b and len(a) == 5
    c = apply_operations(rows, [{"op": "shuffle"}], seed=42)
    assert sorted(r["i"] for r in c) == list(range(10))


def test_sample_larger_than_rows_returns_all():
    rows = [{"i": 1}]
    assert apply_operations(rows, [{"op": "sample", "n": 99}]) == rows


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="未知操作"):
        apply_operations(ROWS, [{"op": "magic"}])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_auto_process.py -v`
Expected: FAIL（`ModuleNotFoundError: app.engine.nodes`）

- [ ] **Step 3: 实现 `backend/app/engine/nodes.py`（第一部分：自动处理）**

```python
import random
import re


def _dedup(rows, op, rng):
    cols = op.get("columns") or []
    seen, out = set(), []
    for row in rows:
        key = tuple(str(row.get(c)) for c in (cols or sorted({k for r in rows for k in r})))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _filter(rows, op, rng):
    col, mode, value = op["column"], op["mode"], op["value"]

    def keep(row) -> bool:
        s = str(row.get(col, ""))
        if mode == "min_len":
            return len(s) >= value
        if mode == "max_len":
            return len(s) <= value
        if mode == "contains":
            return value in s
        if mode == "not_contains":
            return value not in s
        if mode == "regex":
            return re.search(value, s) is not None
        raise ValueError(f"未知过滤模式: {mode}")

    return [r for r in rows if keep(r)]


def _rename(rows, op, rng):
    mapping = op["mapping"]
    return [{mapping.get(k, k): v for k, v in r.items()} for r in rows]


def _drop(rows, op, rng):
    cols = set(op["columns"])
    return [{k: v for k, v in r.items() if k not in cols} for r in rows]


def _concat(rows, op, rng):
    sep = op.get("sep", "")
    return [{**r, op["target"]: sep.join(str(r.get(c, "")) for c in op["columns"])} for r in rows]


def _cast(rows, op, rng):
    caster = {"str": str, "int": int, "float": float}[op["to"]]
    return [{**r, op["column"]: caster(r.get(op["column"]))} for r in rows]


def _sample(rows, op, rng):
    n = op["n"]
    return rows if n >= len(rows) else rng.sample(rows, n)


def _shuffle(rows, op, rng):
    out = list(rows)
    rng.shuffle(out)
    return out


_OPS = {"dedup": _dedup, "filter": _filter, "rename": _rename, "drop": _drop,
        "concat": _concat, "cast": _cast, "sample": _sample, "shuffle": _shuffle}


def apply_operations(rows: list[dict], operations: list[dict], seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    for op in operations:
        fn = _OPS.get(op.get("op"))
        if fn is None:
            raise ValueError(f"未知操作: {op.get('op')}")
        rows = fn(rows, op, rng)
    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_auto_process.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: 自动处理节点操作函数"
```

---

### Task 10: LLM 合成行执行器（模板/扇出/输出映射）

**Files:**
- Modify: `backend/app/engine/nodes.py`（追加第二部分）
- Test: `backend/tests/test_llm_synth.py`

llm_synth 节点 config 结构（全计划统一）：

```json
{
  "model_config_id": 1,
  "system_prompt": "你是数据合成助手",
  "user_prompt": "把这个问题改写得更难：{{q}}",
  "params": {"temperature": 0.8, "top_p": null, "max_tokens": 2048, "json_mode": false, "timeout": 120},
  "concurrency": 4,
  "fanout_n": 1,
  "output_mode": "column",
  "output_column": "output",
  "retries": 3
}
```

- [ ] **Step 1: 写失败测试 `backend/tests/test_llm_synth.py`**

```python
import asyncio
import json

import pytest

from app.engine import nodes
from app.models import ModelConfig
from app.services import llm


def mc():
    from app import crypto
    return ModelConfig(user_id=1, name="m", model_name="qwen", base_url="http://x/v1",
                       api_key_enc=crypto.encrypt("k"), default_params_json="{}")


def patch_chat(monkeypatch, fn):
    async def fake_chat(mc_, system, user, params=None, retries=3):
        return fn(system, user)
    monkeypatch.setattr(llm, "chat", fake_chat)


def test_render_template():
    assert nodes.render_template("改写：{{q}}，难度{{ level }}", {"q": "你好", "level": 5}) == "改写：你好，难度5"
    assert nodes.render_template("缺列：{{nope}}!", {}) == "缺列：!"


async def test_column_mode(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: (f"回答[{u}]", {"prompt_tokens": 3, "completion_tokens": 7}))
    config = {"system_prompt": "sys", "user_prompt": "Q: {{q}}", "output_mode": "column",
              "output_column": "answer"}
    out, usage = await nodes.run_llm_synth_row(config, {"q": "你好"}, mc(), asyncio.Semaphore(8))
    assert out == [{"q": "你好", "answer": "回答[Q: 你好]"}]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 7}


async def test_json_mode_merges_columns(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: (json.dumps({"a": 1, "b": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}))
    config = {"user_prompt": "u", "output_mode": "json"}
    out, _ = await nodes.run_llm_synth_row(config, {"q": "原"}, mc(), asyncio.Semaphore(8))
    assert out == [{"q": "原", "a": 1, "b": "x"}]


async def test_json_mode_non_object_raises(monkeypatch):
    patch_chat(monkeypatch, lambda s, u: ("[1,2]", {"prompt_tokens": 1, "completion_tokens": 1}))
    with pytest.raises(ValueError, match="JSON 对象"):
        await nodes.run_llm_synth_row({"user_prompt": "u", "output_mode": "json"}, {}, mc(), asyncio.Semaphore(8))


async def test_fanout(monkeypatch):
    counter = {"n": 0}

    def fn(s, u):
        counter["n"] += 1
        return f"变体{counter['n']}", {"prompt_tokens": 1, "completion_tokens": 2}

    patch_chat(monkeypatch, fn)
    config = {"user_prompt": "u", "fanout_n": 3, "output_column": "v"}
    out, usage = await nodes.run_llm_synth_row(config, {"q": 1}, mc(), asyncio.Semaphore(8))
    assert len(out) == 3
    assert {r["v"] for r in out} == {"变体1", "变体2", "变体3"}
    assert usage == {"prompt_tokens": 3, "completion_tokens": 6}


async def test_semaphore_limits_concurrency(monkeypatch):
    state = {"now": 0, "peak": 0}

    async def fake_chat(mc_, system, user, params=None, retries=3):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", fake_chat)
    config = {"user_prompt": "u", "fanout_n": 10, "output_column": "v"}
    await nodes.run_llm_synth_row(config, {}, mc(), asyncio.Semaphore(2))
    assert state["peak"] <= 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_llm_synth.py -v`
Expected: FAIL（`AttributeError: render_template`）

- [ ] **Step 3: 在 `backend/app/engine/nodes.py` 追加实现**

```python
import asyncio
import json as _json

from app.models import ModelConfig
from app.services import llm

TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def render_template(template: str, row: dict) -> str:
    return TEMPLATE_RE.sub(lambda m: str(row.get(m.group(1), "")), template)


async def run_llm_synth_row(config: dict, row: dict, mc: ModelConfig,
                            user_sem: asyncio.Semaphore) -> tuple[list[dict], dict]:
    """处理一条输入行：扇出 fanout_n 次调用，返回 (输出行列表, usage 汇总)。失败抛异常由 runner 记为行失败。"""
    system = render_template(config.get("system_prompt", ""), row)
    user = render_template(config.get("user_prompt", ""), row)
    params = config.get("params", {})
    retries = config.get("retries", 3)
    fanout = config.get("fanout_n", 1)

    async def one() -> tuple[str, dict]:
        async with user_sem:
            return await llm.chat(mc, system, user, params=params, retries=retries)

    results = await asyncio.gather(*[one() for _ in range(fanout)])

    out_rows: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    for text, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        if config.get("output_mode") == "json":
            parsed = _json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("LLM 返回的不是 JSON 对象")
            out_rows.append({**row, **parsed})
        else:
            out_rows.append({**row, config.get("output_column", "output"): text})
    return out_rows, usage_total
```

注意：文件顶部已有 `import re`；新增 import 合并到顶部。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_llm_synth.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: LLM 合成行执行器（模板/扇出/输出映射）"
```

---

### Task 11: 执行引擎 runner（拓扑执行/断点/取消/进度落库）

**Files:**
- Create: `backend/app/engine/runner.py`
- Test: `backend/tests/test_runner.py`

语义回顾：节点按拓扑序执行。llm_synth 为行级（节点内并发、每输入行一条 run_rows 记录、可断点）；input/auto_process/output 为批级（单条 `row_idx=0` 记录）。批级节点失败 → 整个运行 failed；行级节点单行失败 → 记录后继续，运行仍可 completed。普通恢复只跳过 done、重做 pending/running，**不重试 failed**（failed 由"重跑失败行"显式重置）。

- [ ] **Step 1: 写失败测试 `backend/tests/test_runner.py`**

```python
import asyncio
import json

from sqlalchemy import select

from app import crypto
from app.engine import runner
from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)
from app.services import llm

GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}


async def make_run(session_factory, graph=None, rows=3) -> int:
    async with session_factory() as s:
        u = User(username=f"runner{id(graph)}")
        s.add(u)
        await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="qwen", base_url="http://x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc)
        await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=rows)
        s.add(ds)
        await s.flush()
        for i in range(rows):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": f"问{i}"}, ensure_ascii=False)))
        g = json.loads(json.dumps(graph or GRAPH))
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
        return run.id


def patch_chat(monkeypatch, fn=None):
    calls: list[str] = []

    async def fake(mc, system, user, params=None, retries=3):
        calls.append(user)
        if fn:
            return fn(user)
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake)
    return calls


async def run_it(session_factory, run_id, cancel=None):
    await runner.execute_run(run_id, session_factory, asyncio.Semaphore(8), cancel or asyncio.Event())


async def get_run(session_factory, run_id) -> Run:
    async with session_factory() as s:
        return await s.get(Run, run_id)


async def test_happy_path(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    assert json.loads(run.stats_json) == {"prompt_tokens": 3, "completion_tokens": 6}
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 3
    assert out_rows[0]["a"] == "答[Q:问0]"
    async with session_factory() as s:
        states = {ns.node_id: ns for ns in (await s.execute(
            select(RunNodeState).where(RunNodeState.run_id == run_id))).scalars()}
    assert states["gen"].total == 3 and states["gen"].done == 3 and states["gen"].failed == 0
    assert states["out"].status == "done"


async def test_row_failure_continues(session_factory, monkeypatch):
    def fn(user):
        if "问1" in user:
            raise RuntimeError("坏行")
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    run_id = await make_run(session_factory)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"  # 单行失败不挂任务
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out_rows) == 2
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "gen", RunRow.status == "failed"))).scalar_one()
    assert "坏行" in rec.error and rec.row_idx == 1


async def test_cancel_before_llm(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    ev = asyncio.Event()
    ev.set()
    await run_it(session_factory, run_id, cancel=ev)
    assert (await get_run(session_factory, run_id)).status == "cancelled"
    assert calls == []


async def test_resume_skips_done_rows(session_factory, monkeypatch):
    calls = patch_chat(monkeypatch)
    run_id = await make_run(session_factory)
    async with session_factory() as s:  # 预置 idx0 已完成（模拟上次中断）
        s.add(RunRow(run_id=run_id, node_id="gen", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "问0", "a": "旧结果"}], ensure_ascii=False)))
        await s.commit()
    await run_it(session_factory, run_id)
    assert sorted(calls) == ["Q:问1", "Q:问2"]  # 只跑了未完成的两行
    out_rows = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["a"] for r in out_rows} == {"旧结果", "答[Q:问1]", "答[Q:问2]"}


async def test_barrier_failure_fails_run(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"].insert(1, {"id": "proc", "type": "auto_process",
                              "config": {"operations": [{"op": "cast", "column": "q", "to": "int"}]}})
    graph["edges"] = [{"source": "in", "target": "proc", "kind": "normal"},
                      {"source": "proc", "target": "gen", "kind": "normal"},
                      {"source": "gen", "target": "out", "kind": "normal"}]
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed"
    assert "invalid literal" in run.error


async def test_fanout_multiplies_rows(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][1]["config"]["fanout_n"] = 2
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    assert len(await runner._node_outputs(session_factory, run_id, "out")) == 6


async def test_output_save_as_dataset(session_factory, monkeypatch):
    patch_chat(monkeypatch)
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["config"] = {"save_as_dataset": True, "dataset_name": "结果集"}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    async with session_factory() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalar_one()
    assert ds.source == "run" and ds.row_count == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_runner.py -v`
Expected: FAIL（`ModuleNotFoundError: app.engine.runner`）

- [ ] **Step 3: 实现 `backend/app/engine/runner.py`**

```python
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine import nodes
from app.engine.graph import Graph, Node, parse_graph, topo_order, upstream_ids, validate_graph
from app.models import DatasetRow, ModelConfig, Run, RunNodeState, RunRow, WorkflowVersion
from app.routers.datasets import create_dataset


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def execute_run(run_id: int, session_factory: async_sessionmaker,
                      user_sem: asyncio.Semaphore, cancel_event: asyncio.Event) -> None:
    """运行入口：任何未捕获异常都落为 run.failed，不向上抛。"""
    try:
        await _execute(run_id, session_factory, user_sem, cancel_event)
    except Exception as e:
        async with session_factory() as s:
            run = await s.get(Run, run_id)
            run.status = "failed"
            run.error = str(e)
            run.finished_at = _now()
            await s.commit()


async def _execute(run_id, session_factory, user_sem, cancel_event):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        run.status = "running"
        run.started_at = run.started_at or _now()
        await s.commit()
        user_id = run.user_id
        stats = json.loads(run.stats_json)
    stats.setdefault("prompt_tokens", 0)
    stats.setdefault("completion_tokens", 0)
    graph = parse_graph(ver.graph_json)
    validate_graph(graph)

    for node in topo_order(graph):
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled", stats)
        inputs = await _node_inputs(session_factory, run_id, graph, node)
        if node.type == "llm_synth":
            await _run_llm_node(session_factory, run_id, user_id, node, inputs,
                                user_sem, cancel_event, stats)
        else:
            await _run_barrier_node(session_factory, run_id, user_id, node, inputs)
        if cancel_event.is_set():
            return await _finish(session_factory, run_id, "cancelled", stats)
    await _finish(session_factory, run_id, "completed", stats)


async def _finish(session_factory, run_id, status, stats):
    async with session_factory() as s:
        run = await s.get(Run, run_id)
        run.status = status
        run.stats_json = json.dumps(stats)
        run.finished_at = _now()
        await s.commit()


async def _node_outputs(session_factory, run_id, node_id) -> list[dict]:
    async with session_factory() as s:
        recs = (await s.execute(
            select(RunRow).where(RunRow.run_id == run_id, RunRow.node_id == node_id,
                                 RunRow.status == "done").order_by(RunRow.row_idx)
        )).scalars().all()
    out: list[dict] = []
    for r in recs:
        out.extend(json.loads(r.data_json))
    return out


async def _node_inputs(session_factory, run_id, graph: Graph, node: Node) -> list[dict]:
    rows: list[dict] = []
    for uid in upstream_ids(graph, node.id):
        rows.extend(await _node_outputs(session_factory, run_id, uid))
    return rows


async def _write_unit(session_factory, run_id, node_id, row_idx, status, out_rows, error):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.row_idx == row_idx
        ))).scalar_one_or_none()
        if rec is None:
            rec = RunRow(run_id=run_id, node_id=node_id, row_idx=row_idx)
            s.add(rec)
        rec.status = status
        rec.data_json = json.dumps(out_rows, ensure_ascii=False)
        rec.error = error
        rec.attempt += 1
        await s.commit()


async def _set_node_state(session_factory, run_id, node_id, *, status, total, done, failed):
    async with session_factory() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == run_id, RunNodeState.node_id == node_id
        ))).scalar_one_or_none()
        if ns is None:
            ns = RunNodeState(run_id=run_id, node_id=node_id)
            s.add(ns)
        ns.status, ns.total, ns.done, ns.failed = status, total, done, failed
        await s.commit()


async def _run_barrier_node(session_factory, run_id, user_id, node: Node, inputs):
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id, RunRow.row_idx == 0
        ))).scalar_one_or_none()
    if rec is not None and rec.status == "done":
        return  # 断点续跑：已完成
    await _set_node_state(session_factory, run_id, node.id, status="running", total=1, done=0, failed=0)
    try:
        out = await _barrier_output(session_factory, user_id, node, inputs)
    except Exception as e:
        await _write_unit(session_factory, run_id, node.id, 0, "failed", [], str(e))
        await _set_node_state(session_factory, run_id, node.id, status="failed", total=1, done=0, failed=1)
        raise
    await _write_unit(session_factory, run_id, node.id, 0, "done", out, "")
    await _set_node_state(session_factory, run_id, node.id, status="done", total=1, done=1, failed=0)


async def _barrier_output(session_factory, user_id, node: Node, inputs) -> list[dict]:
    cfg = node.config
    if node.type == "input":
        rows: list[dict] = []
        async with session_factory() as s:
            for ds_id in cfg.get("dataset_ids", []):
                recs = (await s.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds_id)
                                        .order_by(DatasetRow.idx))).scalars().all()
                rows.extend(json.loads(r.data_json) for r in recs)
        return rows
    if node.type == "auto_process":
        return nodes.apply_operations(inputs, cfg.get("operations", []), seed=cfg.get("seed"))
    if node.type == "output":
        if cfg.get("save_as_dataset"):
            async with session_factory() as s:
                await create_dataset(s, user_id, cfg.get("dataset_name", "运行结果"),
                                     inputs, source="run")
        return inputs
    raise ValueError(f"未知节点类型: {node.type}")


async def _run_llm_node(session_factory, run_id, user_id, node: Node, inputs,
                        user_sem, cancel_event, stats):
    cfg = node.config
    async with session_factory() as s:
        mc = await s.get(ModelConfig, cfg.get("model_config_id"))
        if mc is None or mc.user_id != user_id:
            raise ValueError(f"节点 {node.id}: 模型配置不存在")
        existing = (await s.execute(select(RunRow.row_idx, RunRow.status).where(
            RunRow.run_id == run_id, RunRow.node_id == node.id))).all()
    done_idx = {idx for idx, st in existing if st == "done"}
    failed_idx = {idx for idx, st in existing if st == "failed"}
    total = len(inputs)
    done_count, failed_count = len(done_idx), len(failed_idx)
    await _set_node_state(session_factory, run_id, node.id, status="running",
                          total=total, done=done_count, failed=failed_count)
    todo = [i for i in range(total) if i not in done_idx and i not in failed_idx]
    node_sem = asyncio.Semaphore(cfg.get("concurrency", 4))
    lock = asyncio.Lock()

    async def work(idx: int):
        nonlocal done_count, failed_count
        async with node_sem:
            if cancel_event.is_set():
                return
            try:
                out_rows, usage = await nodes.run_llm_synth_row(cfg, inputs[idx], mc, user_sem)
                await _write_unit(session_factory, run_id, node.id, idx, "done", out_rows, "")
                async with lock:
                    stats["prompt_tokens"] += usage["prompt_tokens"]
                    stats["completion_tokens"] += usage["completion_tokens"]
                    done_count += 1
            except Exception as e:
                await _write_unit(session_factory, run_id, node.id, idx, "failed", [], str(e))
                async with lock:
                    failed_count += 1
            await _set_node_state(session_factory, run_id, node.id, status="running",
                                  total=total, done=done_count, failed=failed_count)

    await asyncio.gather(*[work(i) for i in todo])
    if not cancel_event.is_set():
        await _set_node_state(session_factory, run_id, node.id, status="done",
                              total=total, done=done_count, failed=failed_count)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_runner.py -v`
Expected: 7 passed

- [ ] **Step 5: 运行全部后端测试**

Run: `uv run pytest`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: 执行引擎——拓扑执行/断点续跑/取消/进度落库"
```

---

### Task 12: RunManager + 运行 API + 导出（后端收口）

**Files:**
- Create: `backend/app/engine/manager.py`
- Create: `backend/app/services/export.py`
- Create: `backend/app/routers/runs.py`
- Modify: `backend/app/engine/graph.py`（追加 `descendants`）
- Modify: `backend/app/main.py`（挂载路由 + 启动恢复）
- Test: `backend/tests/test_runs_api.py`
- Test: `backend/tests/test_graph.py`（追加 descendants 测试）

**rerun-failed 的关键语义**：失败行重置为 pending 还不够——下游节点可能已用"残缺数据"跑完（断点会跳过它们）。所以必须把**所有含失败行节点的下游节点**的 run_rows/run_node_states 全部删除重算。

- [ ] **Step 1: 在 test_graph.py 追加 descendants 测试**

```python
def test_descendants():
    from app.engine.graph import descendants
    nodes = [("a", "input"), ("b", "llm_synth"), ("c", "auto_process"), ("d", "output")]
    edges = [("a", "b", "normal"), ("b", "c", "normal"), ("c", "d", "normal")]
    graph = g(nodes, edges)
    assert descendants(graph, "b") == {"c", "d"}
    assert descendants(graph, "d") == set()
```

- [ ] **Step 2: 在 `backend/app/engine/graph.py` 追加实现**

```python
def descendants(g: Graph, node_id: str) -> set[str]:
    """沿 normal 边可达的所有下游节点 id（不含自身）。"""
    out: set[str] = set()
    frontier = [node_id]
    while frontier:
        nid = frontier.pop()
        for e in g.edges:
            if e["kind"] == "normal" and e["source"] == nid and e["target"] not in out:
                out.add(e["target"])
                frontier.append(e["target"])
    return out
```

Run: `uv run pytest tests/test_graph.py -v` → 全部通过后继续。

- [ ] **Step 3: 写失败测试 `backend/tests/test_runs_api.py`**

```python
import asyncio
import json

from app.services import llm

GRAPH_TEMPLATE = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth",
         "config": {"model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a",
                    "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}],
}
JSONL = '{"q": "问0"}\n{"q": "问1"}\n{"q": "问2"}\n'.encode("utf-8")


def patch_chat(monkeypatch, fn=None):
    async def fake(mc, system, user, params=None, retries=3):
        if fn:
            return fn(user)
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake)


async def setup_workflow(client) -> int:
    files = [("files", ("种子.jsonl", JSONL, "application/octet-stream"))]
    ds = (await client.post("/api/datasets/upload", files=files)).json()[0]
    mc = (await client.post("/api/models", json={
        "name": "m", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await client.post("/api/workflows", json={"name": "流"})).json()
    graph = json.loads(json.dumps(GRAPH_TEMPLATE))
    graph["nodes"][0]["config"]["dataset_ids"] = [ds["id"]]
    graph["nodes"][1]["config"]["model_config_id"] = mc["id"]
    await client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    return wf["id"]


async def wait_run(client, run_id, timeout=5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        r = (await client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("运行未在限期内结束")


async def test_run_end_to_end(auth_client, monkeypatch):
    patch_chat(monkeypatch)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    assert detail["status"] == "completed"
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state == {"node_id": "gen", "status": "done", "total": 3, "done": 3, "failed": 0}
    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()
    assert rows["total"] == 1  # 批级节点 1 个工作单元
    assert len(rows["rows"]) == 3 and rows["rows"][0]["a"] == "答[Q:问0]"
    exp = await auth_client.get(f"/api/runs/{run_id}/export?format=jsonl")
    assert exp.status_code == 200
    lines = [json.loads(line) for line in exp.text.strip().splitlines()]
    assert len(lines) == 3 and lines[0]["a"] == "答[Q:问0]"
    listed = (await auth_client.get("/api/runs")).json()
    assert listed[0]["workflow_name"] == "流"


async def test_create_run_invalid_graph(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "空"})).json()
    r = await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})
    assert r.status_code == 422


async def test_create_run_foreign_dataset_rejected(auth_client):
    await auth_client.post("/api/auth/login", json={"username": "other"})
    files = [("files", ("a.jsonl", JSONL, "application/octet-stream"))]
    foreign_ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    await auth_client.post("/api/auth/login", json={"username": "tester"})
    wf_id = await setup_workflow(auth_client)
    wf = (await auth_client.get(f"/api/workflows/{wf_id}")).json()
    wf["graph"]["nodes"][0]["config"]["dataset_ids"] = [foreign_ds["id"]]
    await auth_client.put(f"/api/workflows/{wf_id}", json={"graph": wf["graph"]})
    r = await auth_client.post("/api/runs", json={"workflow_id": wf_id})
    assert r.status_code == 422


async def test_rerun_failed(auth_client, monkeypatch):
    broken = {"on": True}

    def fn(user):
        if broken["on"] and "问1" in user:
            raise RuntimeError("临时故障")
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 1}

    patch_chat(monkeypatch, fn)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["failed"] == 1
    failed = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=gen&status=failed")).json()
    assert failed["rows"][0]["error"] == "临时故障"

    broken["on"] = False  # 故障修复后重跑失败行
    assert (await auth_client.post(f"/api/runs/{run_id}/rerun-failed")).status_code == 200
    detail = await wait_run(auth_client, run_id)
    gen_state = next(s for s in detail["node_states"] if s["node_id"] == "gen")
    assert gen_state["done"] == 3 and gen_state["failed"] == 0
    rows = (await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")).json()
    assert len(rows["rows"]) == 3  # 下游已重算，包含修复行


async def test_cancel_running(auth_client, monkeypatch):
    async def slow(mc, system, user, params=None, retries=3):
        await asyncio.sleep(0.2)
        return "ok", {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(llm, "chat", slow)
    wf_id = await setup_workflow(auth_client)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await auth_client.post(f"/api/runs/{run_id}/cancel")
    detail = await wait_run(auth_client, run_id)
    assert detail["status"] == "cancelled"


async def test_startup_resume(auth_client, monkeypatch, session_factory):
    from app.engine import manager as manager_mod
    from app.models import Run, User, Workflow, WorkflowVersion

    async with session_factory() as s:
        u = User(username="resumer")
        s.add(u)
        await s.flush()
        wf = Workflow(user_id=u.id, name="w", graph_json="{}")
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}")
        s.add(ver)
        await s.flush()
        s.add(Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="running"))
        await s.commit()

    resumed = []

    async def fake_execute(run_id, sf, sem, ev):
        resumed.append(run_id)

    monkeypatch.setattr(manager_mod, "execute_run", fake_execute)
    count = await manager_mod.resume_unfinished(session_factory)
    assert count == 1
    await asyncio.sleep(0)  # 让 create_task 调度
    assert len(resumed) == 1
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/test_runs_api.py -v`
Expected: FAIL（`ModuleNotFoundError: app.engine.manager`）

- [ ] **Step 5: 实现 `backend/app/engine/manager.py`**

```python
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine.runner import execute_run
from app.models import Run, User


class RunManager:
    """运行任务的进程内登记处：取消事件、用户信号量、后台 asyncio.Task。"""

    def __init__(self):
        self.user_sems: dict[int, asyncio.Semaphore] = {}
        self.cancel_events: dict[int, asyncio.Event] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    def user_sem(self, user_id: int, capacity: int) -> asyncio.Semaphore:
        if user_id not in self.user_sems:
            self.user_sems[user_id] = asyncio.Semaphore(capacity)
        return self.user_sems[user_id]

    def submit(self, run_id: int, user_id: int, capacity: int,
               session_factory: async_sessionmaker) -> None:
        ev = asyncio.Event()
        self.cancel_events[run_id] = ev
        task = asyncio.create_task(
            execute_run(run_id, session_factory, self.user_sem(user_id, capacity), ev))
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self._cleanup(run_id))

    def _cleanup(self, run_id: int) -> None:
        self.cancel_events.pop(run_id, None)
        self.tasks.pop(run_id, None)

    def cancel(self, run_id: int) -> None:
        ev = self.cancel_events.get(run_id)
        if ev:
            ev.set()


manager = RunManager()


async def resume_unfinished(session_factory: async_sessionmaker) -> int:
    """进程启动时恢复 queued/running 的运行（断点续跑）。返回恢复数量。"""
    async with session_factory() as s:
        rows = (await s.execute(
            select(Run, User).join(User, Run.user_id == User.id)
            .where(Run.status.in_(("queued", "running")))
        )).all()
    for run, user in rows:
        manager.submit(run.id, user.id, user.max_llm_concurrency, session_factory)
    return len(rows)
```

注意 `resume_unfinished` 调用的 `execute_run` 是 manager 模块命名空间里的引用（`from ... import execute_run`），测试据此 monkeypatch。

- [ ] **Step 6: 实现 `backend/app/services/export.py`**

```python
import json
from pathlib import Path

import pandas as pd


def export_rows(rows: list[dict], fmt: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                        encoding="utf-8")
    elif fmt == "csv":
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    elif fmt == "xlsx":
        pd.DataFrame(rows).to_excel(path, index=False)
    else:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return path
```

- [ ] **Step 7: 实现 `backend/app/routers/runs.py`**

```python
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session, get_session_factory
from app.engine.graph import GraphError, descendants, parse_graph, validate_graph
from app.engine.manager import manager
from app.models import (Dataset, ModelConfig, Run, RunNodeState, RunRow, User,
                        Workflow, WorkflowVersion)
from app.routers.workflows import get_owned_workflow
from app.services.export import export_rows

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunCreate(BaseModel):
    workflow_id: int


async def _get_owned_run(run_id: int, user: User, session: AsyncSession) -> Run:
    run = await session.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="运行不存在")
    return run


@router.post("")
async def create_run(body: RunCreate, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(body.workflow_id, user, session)
    graph = parse_graph(wf.graph_json)
    try:
        validate_graph(graph)
        if not graph.nodes:
            raise GraphError("工作流为空")
    except GraphError as e:
        raise HTTPException(status_code=422, detail=str(e))
    for n in graph.nodes:  # 资源归属校验（会话隔离）
        if n.type == "input":
            for ds_id in n.config.get("dataset_ids", []):
                ds = await session.get(Dataset, ds_id)
                if ds is None or ds.user_id != user.id:
                    raise HTTPException(status_code=422, detail=f"节点 {n.id}: 数据集不存在")
        if n.type == "llm_synth":
            mc = await session.get(ModelConfig, n.config.get("model_config_id"))
            if mc is None or mc.user_id != user.id:
                raise HTTPException(status_code=422, detail=f"节点 {n.id}: 未选择有效的模型配置")
    max_ver = (await session.execute(select(func.max(WorkflowVersion.version)).where(
        WorkflowVersion.workflow_id == wf.id))).scalar() or 0
    ver = WorkflowVersion(workflow_id=wf.id, version=max_ver + 1, graph_json=wf.graph_json)
    session.add(ver)
    await session.flush()
    run = Run(user_id=user.id, workflow_id=wf.id, workflow_version_id=ver.id)
    session.add(run)
    await session.commit()
    manager.submit(run.id, user.id, user.max_llm_concurrency, get_session_factory())
    return {"id": run.id, "status": run.status}


def _run_out(run: Run, workflow_name: str = "") -> dict:
    return {
        "id": run.id, "workflow_id": run.workflow_id, "workflow_name": workflow_name,
        "status": run.status, "error": run.error, "stats": json.loads(run.stats_json),
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("")
async def list_runs(workflow_id: int | None = None, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    stmt = (select(Run, Workflow.name).join(Workflow, Run.workflow_id == Workflow.id)
            .where(Run.user_id == user.id).order_by(Run.id.desc()))
    if workflow_id is not None:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    rows = (await session.execute(stmt)).all()
    return [_run_out(run, name) for run, name in rows]


@router.get("/{run_id}")
async def run_detail(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    states = (await session.execute(
        select(RunNodeState).where(RunNodeState.run_id == run.id))).scalars().all()
    return {**_run_out(run, wf.name if wf else ""), "graph": json.loads(ver.graph_json),
            "node_states": [{"node_id": s.node_id, "status": s.status, "total": s.total,
                             "done": s.done, "failed": s.failed} for s in states]}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"当前状态 {run.status} 不可取消")
    manager.cancel(run.id)
    return {"ok": True}


@router.post("/{run_id}/rerun-failed")
async def rerun_failed(run_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    if run.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="运行尚未结束")
    failed_nodes = (await session.execute(
        select(RunRow.node_id).where(RunRow.run_id == run.id, RunRow.status == "failed")
        .distinct())).scalars().all()
    if not failed_nodes:
        raise HTTPException(status_code=409, detail="没有失败行")
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    graph = parse_graph(ver.graph_json)
    reset_targets: set[str] = set()
    for nid in failed_nodes:
        reset_targets |= descendants(graph, nid)
    await session.execute(update(RunRow).where(
        RunRow.run_id == run.id, RunRow.status == "failed"
    ).values(status="pending", error=""))
    if reset_targets:
        await session.execute(sa_delete(RunRow).where(
            RunRow.run_id == run.id, RunRow.node_id.in_(reset_targets)))
        await session.execute(sa_delete(RunNodeState).where(
            RunNodeState.run_id == run.id, RunNodeState.node_id.in_(reset_targets)))
    run.status = "queued"
    run.error = ""
    run.finished_at = None
    await session.commit()
    manager.submit(run.id, user.id, user.max_llm_concurrency, get_session_factory())
    return {"ok": True}


def _flatten(recs: list[RunRow]) -> list[dict]:
    rows: list[dict] = []
    for r in recs:
        rows.extend(json.loads(r.data_json))
    return rows


@router.get("/{run_id}/rows")
async def run_rows(run_id: int, node_id: str, status: str = "done",
                   page: int = 1, page_size: int = 20,
                   user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    await _get_owned_run(run_id, user, session)
    base = (RunRow.run_id == run_id, RunRow.node_id == node_id, RunRow.status == status)
    total = (await session.execute(
        select(func.count()).select_from(RunRow).where(*base))).scalar()
    recs = (await session.execute(
        select(RunRow).where(*base).order_by(RunRow.row_idx)
        .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    if status == "failed":
        return {"total": total, "rows": [
            {"row_idx": r.row_idx, "error": r.error, "attempt": r.attempt} for r in recs]}
    return {"total": total, "rows": _flatten(recs)}


@router.get("/{run_id}/export")
async def export_run(run_id: int, node_id: str | None = None, format: str = "jsonl",
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _get_owned_run(run_id, user, session)
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    graph = parse_graph(ver.graph_json)
    if node_id is None:
        outputs = [n for n in graph.nodes if n.type == "output"]
        if not outputs:
            raise HTTPException(status_code=422, detail="工作流没有输出节点")
        node_id = outputs[0].id
    recs = (await session.execute(
        select(RunRow).where(RunRow.run_id == run.id, RunRow.node_id == node_id,
                             RunRow.status == "done").order_by(RunRow.row_idx))).scalars().all()
    filename = f"run{run.id}_{node_id}.{format}"
    path = export_rows(_flatten(recs), format, settings.data_dir / "exports" / filename)
    return FileResponse(path, filename=filename)
```

- [ ] **Step 8: 更新 `backend/app/main.py`（最终形态）**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import get_session_factory, init_db
from app.engine.manager import resume_unfinished
from app.routers import auth, datasets, model_configs, runs, workflows


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await resume_unfinished(get_session_factory())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GraphFlow", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(model_configs.router)
    app.include_router(datasets.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)
    return app


app = create_app()
```

- [ ] **Step 9: 运行全部后端测试**

Run: `uv run pytest -v`
Expected: 全部通过（约 45 个用例）

- [ ] **Step 10: 手动冒烟（可选但推荐）**

Run: `uv run fastapi dev app/main.py`，浏览器开 `http://127.0.0.1:8000/docs`，走一遍 login → 上传 → 建流 → 运行。

- [ ] **Step 11: 提交**

```bash
git add backend/
git commit -m "feat: 运行管理器、运行 API 与导出——后端 P1 完成"
```

---

### Task 13: 前端脚手架 + API 客户端 + 登录 + 布局路由

**Files:**
- Create: `frontend/`（Vite react-ts 模板）
- Create: `frontend/src/api/client.ts`、`frontend/src/api/types.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/pages/LoginPage.tsx`
- Modify: `frontend/src/App.tsx`、`frontend/src/main.tsx`、`frontend/vite.config.ts`
- Test: `frontend/src/api/client.test.ts`

- [ ] **Step 1: 脚手架与依赖**

```bash
npm create vite@latest frontend -- --template react-ts
cd frontend
npm i antd @xyflow/react zustand react-router-dom
npm i -D vitest jsdom @testing-library/react @testing-library/jest-dom
```

删除模板自带的 `src/App.css`、`src/assets`，`src/index.css` 清空为 `body { margin: 0; }`。

- [ ] **Step 2: `frontend/vite.config.ts`（代理 + vitest）**

```ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: { outDir: '../backend/static', emptyOutDir: true },
  test: { environment: 'jsdom', globals: true },
})
```

`package.json` scripts 追加 `"test": "vitest run"`。

- [ ] **Step 3: 写失败测试 `frontend/src/api/client.test.ts`**

```ts
import { describe, expect, it, vi, afterEach } from 'vitest'
import { api, ApiError } from './client'

afterEach(() => vi.restoreAllMocks())

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status })))
}

describe('api client', () => {
  it('返回 JSON 数据', async () => {
    mockFetch(200, { id: 1 })
    expect(await api.get<{ id: number }>('/api/me')).toEqual({ id: 1 })
  })

  it('非 2xx 抛 ApiError 并带后端 detail', async () => {
    mockFetch(422, { detail: '数据集不存在' })
    await expect(api.post('/api/runs', { workflow_id: 1 })).rejects.toThrowError(
      new ApiError(422, '数据集不存在'),
    )
  })
})
```

- [ ] **Step 4: 运行测试确认失败**

Run: `npm test`
Expected: FAIL（client.ts 不存在）

- [ ] **Step 5: 实现 `frontend/src/api/client.ts`**

```ts
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = init?.body instanceof FormData ? undefined : { 'Content-Type': 'application/json' }
  const res = await fetch(path, { headers, ...init })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = String((await res.json()).detail ?? detail)
    } catch {
      /* 非 JSON 错误体，保留 statusText */
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postForm: <T>(p: string, form: FormData) => request<T>(p, { method: 'POST', body: form }),
  put: <T>(p: string, body: unknown) => request<T>(p, { method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(p: string) => request<T>(p, { method: 'DELETE' }),
}
```

- [ ] **Step 6: 实现 `frontend/src/api/types.ts`**

```ts
export interface UserInfo { id: number; username: string; display_name: string }

export interface ModelConfig {
  id: number; name: string; model_name: string; base_url: string
  api_key_set: boolean; default_params: Record<string, unknown>
}

export interface Dataset {
  id: number; name: string; source: string; original_filename: string
  row_count: number; columns: string[]; created_at: string
}

export interface WorkflowSummary { id: number; name: string; updated_at: string }

export interface GraphNode {
  id: string; type: 'input' | 'llm_synth' | 'auto_process' | 'output'
  position: { x: number; y: number }; config: Record<string, any>
}
export interface GraphEdge { source: string; target: string; kind: 'normal' | 'rescan' }
export interface WorkflowGraph { nodes: GraphNode[]; edges: GraphEdge[] }
export interface Workflow { id: number; name: string; graph: WorkflowGraph; updated_at: string }

export interface NodeState { node_id: string; status: string; total: number; done: number; failed: number }
export interface Run {
  id: number; workflow_id: number; workflow_name: string; status: string; error: string
  stats: { prompt_tokens?: number; completion_tokens?: number }
  created_at: string; finished_at: string | null
}
export interface RunDetail extends Run { graph: WorkflowGraph; node_states: NodeState[] }
export interface RowsPage { total: number; rows: Record<string, any>[] }
```

- [ ] **Step 7: 实现 `frontend/src/stores/auth.ts`**

```ts
import { create } from 'zustand'
import { api } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  ready: boolean
  init: () => Promise<void>
  login: (username: string) => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,
  init: async () => {
    try {
      set({ user: await api.get<UserInfo>('/api/me'), ready: true })
    } catch {
      set({ user: null, ready: true })
    }
  },
  login: async (username) => {
    set({ user: await api.post<UserInfo>('/api/auth/login', { username }) })
  },
}))
```

- [ ] **Step 8: 实现 `frontend/src/pages/LoginPage.tsx`**

```tsx
import { useState } from 'react'
import { Button, Card, Input, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../stores/auth'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const login = useAuth((s) => s.login)
  const navigate = useNavigate()

  const submit = async () => {
    if (!username.trim()) return
    try {
      await login(username.trim())
      navigate('/')
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: '20vh' }}>
      <Card title="GraphFlow 登录" style={{ width: 360 }}>
        <Input
          placeholder="输入用户名（开发模式）"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          onPressEnter={submit}
          autoFocus
        />
        <Button type="primary" block style={{ marginTop: 16 }} onClick={submit}>
          进入
        </Button>
      </Card>
    </div>
  )
}
```

- [ ] **Step 9: 实现 `frontend/src/App.tsx` 与 `frontend/src/main.tsx`**

`App.tsx`：

```tsx
import { useEffect } from 'react'
import { Layout, Menu, Spin } from 'antd'
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import ModelsPage from './pages/ModelsPage'
import DatasetsPage from './pages/DatasetsPage'
import WorkflowsPage from './pages/WorkflowsPage'
import CanvasPage from './pages/CanvasPage'
import RunsPage from './pages/RunsPage'
import RunDetailPage from './pages/RunDetailPage'
import { useAuth } from './stores/auth'

function Shell() {
  const { user, ready } = useAuth()
  const location = useLocation()
  if (!ready) return <Spin style={{ display: 'block', marginTop: '20vh' }} />
  if (!user) return <Navigate to="/login" replace />
  const selected = '/' + (location.pathname.split('/')[1] || 'workflows')
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider theme="light">
        <div style={{ padding: 16, fontWeight: 700 }}>GraphFlow</div>
        <Menu
          selectedKeys={[selected]}
          items={[
            { key: '/workflows', label: <Link to="/workflows">工作流</Link> },
            { key: '/datasets', label: <Link to="/datasets">数据集</Link> },
            { key: '/models', label: <Link to="/models">模型配置</Link> },
            { key: '/runs', label: <Link to="/runs">运行记录</Link> },
          ]}
        />
      </Layout.Sider>
      <Layout.Content style={{ padding: 16 }}>
        <Outlet />
      </Layout.Content>
    </Layout>
  )
}

export default function App() {
  const init = useAuth((s) => s.init)
  useEffect(() => {
    void init()
  }, [init])
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<Shell />}>
          <Route path="/" element={<Navigate to="/workflows" replace />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:id/canvas" element={<CanvasPage />} />
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
```

`main.tsx`：

```tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
```

本任务先为未实现的页面建占位文件（每个 `export default () => <div>TODO 页面</div>` 形式会触发"无占位"红线——所以**本步直接建空壳页面，下个任务填充**；空壳形如：

```tsx
export default function ModelsPage() {
  return <div>模型配置（Task 14 实现）</div>
}
```

`ModelsPage / DatasetsPage / WorkflowsPage / CanvasPage / RunsPage / RunDetailPage` 各一个空壳文件，Task 14-16 逐个替换为完整实现。

- [ ] **Step 10: 测试与手动验证**

Run: `npm test`
Expected: client 2 个用例通过

Run: `npm run dev`（后端同时 `uv run fastapi dev app/main.py`）
Expected: 浏览器登录后看到侧边栏布局，菜单可切换。

- [ ] **Step 11: 提交**

```bash
git add frontend/
git commit -m "feat: 前端脚手架、登录与布局路由"
```

---

### Task 14: 模型配置页 + 数据集页

**Files:**
- Replace: `frontend/src/pages/ModelsPage.tsx`
- Replace: `frontend/src/pages/DatasetsPage.tsx`

前端页面以手动验证为主（画布之外的 CRUD 页都是 antd 标准组装，自动化测试价值低——KISS）。

- [ ] **Step 1: 实现 `frontend/src/pages/ModelsPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { Button, Form, Input, InputNumber, Modal, Popconfirm, Space, Table, message } from 'antd'
import { api } from '../api/client'
import type { ModelConfig } from '../api/types'

interface FormValues {
  name: string; model_name: string; base_url: string; api_key?: string
  temperature?: number; top_p?: number; max_tokens?: number
}

export default function ModelsPage() {
  const [list, setList] = useState<ModelConfig[]>([])
  const [editing, setEditing] = useState<ModelConfig | null | 'new'>(null)
  const [form] = Form.useForm<FormValues>()

  const reload = () => api.get<ModelConfig[]>('/api/models').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  const openEdit = (mc: ModelConfig | 'new') => {
    setEditing(mc)
    if (mc === 'new') form.resetFields()
    else form.setFieldsValue({ ...mc, ...(mc.default_params as object) })
  }

  const save = async () => {
    const v = await form.validateFields()
    const payload = {
      name: v.name, model_name: v.model_name, base_url: v.base_url, api_key: v.api_key ?? '',
      default_params: { temperature: v.temperature, top_p: v.top_p, max_tokens: v.max_tokens },
    }
    if (editing === 'new') await api.post('/api/models', payload)
    else await api.put(`/api/models/${(editing as ModelConfig).id}`, payload)
    setEditing(null)
    await reload()
  }

  const testConn = async (id: number) => {
    const r = await api.post<{ ok: boolean; reply?: string; error?: string }>(`/api/models/${id}/test`)
    if (r.ok) message.success(`连通正常：${r.reply}`)
    else message.error(`连接失败：${r.error}`)
  }

  return (
    <>
      <Button type="primary" onClick={() => openEdit('new')} style={{ marginBottom: 16 }}>
        新增模型
      </Button>
      <Table
        rowKey="id"
        dataSource={list}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '模型 ID', dataIndex: 'model_name' },
          { title: 'Base URL', dataIndex: 'base_url' },
          { title: 'Key', dataIndex: 'api_key_set', render: (v: boolean) => (v ? '已配置' : '未配置') },
          {
            title: '操作',
            render: (_, mc) => (
              <Space>
                <a onClick={() => void testConn(mc.id)}>测试</a>
                <a onClick={() => openEdit(mc)}>编辑</a>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/models/${mc.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal title={editing === 'new' ? '新增模型' : '编辑模型'} open={editing !== null}
             onOk={() => void save()} onCancel={() => setEditing(null)} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
            <Input placeholder="如：内网 Qwen" />
          </Form.Item>
          <Form.Item name="model_name" label="模型 ID" rules={[{ required: true }]}>
            <Input placeholder="如：qwen-max" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="如：http://10.0.0.1:8000/v1" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" extra="编辑时留空表示不修改">
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Space>
            <Form.Item name="temperature" label="temperature">
              <InputNumber min={0} max={2} step={0.1} />
            </Form.Item>
            <Form.Item name="top_p" label="top_p">
              <InputNumber min={0} max={1} step={0.05} />
            </Form.Item>
            <Form.Item name="max_tokens" label="max_tokens">
              <InputNumber min={1} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </>
  )
}
```

- [ ] **Step 2: 实现 `frontend/src/pages/DatasetsPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { Button, Drawer, Popconfirm, Space, Table, Upload, message } from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import { api } from '../api/client'
import type { Dataset, RowsPage } from '../api/types'

export default function DatasetsPage() {
  const [list, setList] = useState<Dataset[]>([])
  const [preview, setPreview] = useState<Dataset | null>(null)
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<RowsPage>({ total: 0, rows: [] })

  const reload = () => api.get<Dataset[]>('/api/datasets').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  useEffect(() => {
    if (preview) void api.get<RowsPage>(`/api/datasets/${preview.id}/rows?page=${page}&page_size=20`).then(setRows)
  }, [preview, page])

  const doUpload = async (files: File[]) => {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    try {
      await api.postForm('/api/datasets/upload', form)
      message.success('上传成功')
      await reload()
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  return (
    <>
      <Upload.Dragger
        multiple
        accept=".jsonl,.json,.csv,.xlsx,.xls"
        beforeUpload={(_, fileList) => {
          void doUpload(fileList as unknown as File[])
          return false
        }}
        showUploadList={false}
        style={{ marginBottom: 16 }}
      >
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p>拖拽或点击上传（支持 JSONL / JSON / CSV / Excel，可多选）</p>
      </Upload.Dragger>
      <Table
        rowKey="id"
        dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '来源', dataIndex: 'source', render: (v: string) => (v === 'run' ? '运行结果' : '上传') },
          { title: '行数', dataIndex: 'row_count' },
          { title: '列', dataIndex: 'columns', render: (cols: string[]) => cols.join(', ') },
          {
            title: '操作',
            render: (_, ds) => (
              <Space>
                <a onClick={() => { setPage(1); setPreview(ds) }}>预览</a>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/datasets/${ds.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={preview?.name} open={!!preview} onClose={() => setPreview(null)} width="60%">
        <Table
          rowKey={(_, i) => String(i)}
          dataSource={rows.rows}
          columns={(preview?.columns ?? []).map((c) => ({
            title: c, dataIndex: c, ellipsis: true,
            render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')),
          }))}
          pagination={{ current: page, pageSize: 20, total: rows.total, onChange: setPage }}
          scroll={{ x: 'max-content' }}
        />
      </Drawer>
    </>
  )
}
```

注意：需要 `npm i @ant-design/icons`（antd 的常用搭配）。

- [ ] **Step 3: 手动验证**

后端起服务，前端 `npm run dev`：新增模型→测试连通（失败提示正常，因为是假地址）；上传一个 jsonl + 一个 csv → 列表出现、预览分页正常、删除正常。

- [ ] **Step 4: 提交**

```bash
git add frontend/
git commit -m "feat: 模型配置页与数据集页"
```

---

### Task 15: 工作流列表 + 画布 + 节点配置表单

**Files:**
- Create: `frontend/src/canvas/serialize.ts`
- Create: `frontend/src/canvas/nodeTypes.tsx`
- Create: `frontend/src/canvas/forms/NodeConfigForm.tsx`
- Replace: `frontend/src/pages/WorkflowsPage.tsx`
- Replace: `frontend/src/pages/CanvasPage.tsx`
- Test: `frontend/src/canvas/serialize.test.ts`

- [ ] **Step 1: 写失败测试 `frontend/src/canvas/serialize.test.ts`**

```ts
import { describe, expect, it } from 'vitest'
import { fromFlow, toFlow } from './serialize'
import type { WorkflowGraph } from '../api/types'

const GRAPH: WorkflowGraph = {
  nodes: [
    { id: 'in_1', type: 'input', position: { x: 0, y: 0 }, config: { dataset_ids: [1] } },
    { id: 'gen_1', type: 'llm_synth', position: { x: 200, y: 0 }, config: { user_prompt: 'Q:{{q}}' } },
  ],
  edges: [{ source: 'in_1', target: 'gen_1', kind: 'normal' }],
}

describe('serialize', () => {
  it('graph → flow → graph 往返一致', () => {
    const f = toFlow(GRAPH)
    expect(fromFlow(f.nodes, f.edges)).toEqual(GRAPH)
  })

  it('flow 边缺少 kind 时默认 normal', () => {
    const g = fromFlow(
      [{ id: 'a', type: 'input', position: { x: 0, y: 0 }, data: { config: {} } }],
      [{ id: 'e1', source: 'a', target: 'a' }],
    )
    expect(g.edges[0].kind).toBe('normal')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm test`
Expected: FAIL（serialize.ts 不存在）

- [ ] **Step 3: 实现 `frontend/src/canvas/serialize.ts`**

```ts
import type { Edge, Node } from '@xyflow/react'
import type { GraphEdge, GraphNode, WorkflowGraph } from '../api/types'

export const NODE_LABELS: Record<GraphNode['type'], string> = {
  input: '输入',
  llm_synth: 'LLM 合成',
  auto_process: '自动处理',
  output: '输出',
}

export function toFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { config: n.config },
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}_${e.source}_${e.target}`, source: e.source, target: e.target,
      data: { kind: e.kind ?? 'normal' },
    })),
  }
}

export function fromFlow(nodes: Node[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.type as GraphNode['type'],
      position: { x: n.position.x, y: n.position.y },
      config: ((n.data as { config?: Record<string, any> })?.config) ?? {},
    })),
    edges: edges.map((e) => ({
      source: e.source, target: e.target,
      kind: (((e.data as { kind?: GraphEdge['kind'] })?.kind) ?? 'normal') as GraphEdge['kind'],
    })),
  }
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `npm test`
Expected: 全部通过

- [ ] **Step 5: 实现 `frontend/src/canvas/nodeTypes.tsx`**

```tsx
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { NODE_LABELS } from './serialize'

const COLORS: Record<string, string> = {
  input: '#1677ff', llm_synth: '#722ed1', auto_process: '#13c2c2', output: '#52c41a',
}

function GFNode({ id, type, selected }: NodeProps) {
  const t = type as keyof typeof NODE_LABELS
  return (
    <div style={{
      background: '#fff', borderRadius: 8, padding: '8px 16px', minWidth: 130,
      border: `2px solid ${COLORS[t]}`, boxShadow: selected ? `0 0 0 3px ${COLORS[t]}55` : 'none',
    }}>
      {t !== 'input' && <Handle type="target" position={Position.Left} />}
      <div style={{ fontSize: 12, color: COLORS[t] }}>{NODE_LABELS[t]}</div>
      <div style={{ fontWeight: 600 }}>{id}</div>
      {t !== 'output' && <Handle type="source" position={Position.Right} />}
    </div>
  )
}

export const nodeTypes = {
  input: GFNode, llm_synth: GFNode, auto_process: GFNode, output: GFNode,
}
```

- [ ] **Step 6: 实现 `frontend/src/canvas/forms/NodeConfigForm.tsx`**

四种节点的配置表单，受控组件直改 config（不引入 antd Form 实例——KISS）：

```tsx
import { useEffect, useState } from 'react'
import { Button, Input, InputNumber, Radio, Select, Space, Switch } from 'antd'
import { api } from '../../api/client'
import type { Dataset, ModelConfig } from '../../api/types'

export interface FormProps {
  config: Record<string, any>
  onChange: (config: Record<string, any>) => void
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ marginBottom: 4, color: '#666' }}>{label}</div>
      {children}
    </div>
  )
}

function InputNodeForm({ config, onChange }: FormProps) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  useEffect(() => {
    void api.get<Dataset[]>('/api/datasets').then(setDatasets)
  }, [])
  return (
    <Field label="数据集（可多选，按行拼接）">
      <Select
        mode="multiple" style={{ width: '100%' }} value={config.dataset_ids ?? []}
        onChange={(v) => onChange({ ...config, dataset_ids: v })}
        options={datasets.map((d) => ({ value: d.id, label: `${d.name}（${d.row_count} 行）` }))}
      />
    </Field>
  )
}

function LlmSynthForm({ config, onChange }: FormProps) {
  const [models, setModels] = useState<ModelConfig[]>([])
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
  return (
    <>
      <Field label="模型">
        <Select
          style={{ width: '100%' }} value={config.model_config_id}
          onChange={(v) => patch({ model_config_id: v })}
          options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))}
        />
      </Field>
      <Field label="System Prompt">
        <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                        onChange={(e) => patch({ system_prompt: e.target.value })} />
      </Field>
      <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
        <Input.TextArea rows={6} value={config.user_prompt ?? ''}
                        onChange={(e) => patch({ user_prompt: e.target.value })} />
      </Field>
      <Field label="输出方式">
        <Radio.Group value={config.output_mode ?? 'column'}
                     onChange={(e) => patch({ output_mode: e.target.value })}>
          <Radio.Button value="column">整段存到列</Radio.Button>
          <Radio.Button value="json">解析 JSON 拆多列</Radio.Button>
        </Radio.Group>
      </Field>
      {(config.output_mode ?? 'column') === 'column' && (
        <Field label="输出列名">
          <Input value={config.output_column ?? 'output'}
                 onChange={(e) => patch({ output_column: e.target.value })} />
        </Field>
      )}
      <Space wrap>
        <Field label="扇出条数"><InputNumber min={1} value={config.fanout_n ?? 1}
          onChange={(v) => patch({ fanout_n: v ?? 1 })} /></Field>
        <Field label="节点并发"><InputNumber min={1} value={config.concurrency ?? 4}
          onChange={(v) => patch({ concurrency: v ?? 4 })} /></Field>
        <Field label="重试次数"><InputNumber min={1} value={config.retries ?? 3}
          onChange={(v) => patch({ retries: v ?? 3 })} /></Field>
      </Space>
      <Space wrap>
        <Field label="temperature"><InputNumber min={0} max={2} step={0.1} value={params.temperature}
          onChange={(v) => patchParams({ temperature: v })} /></Field>
        <Field label="top_p"><InputNumber min={0} max={1} step={0.05} value={params.top_p}
          onChange={(v) => patchParams({ top_p: v })} /></Field>
        <Field label="max_tokens"><InputNumber min={1} value={params.max_tokens}
          onChange={(v) => patchParams({ max_tokens: v })} /></Field>
        <Field label="超时(秒)"><InputNumber min={1} value={params.timeout ?? 120}
          onChange={(v) => patchParams({ timeout: v ?? 120 })} /></Field>
        <Field label="JSON 模式"><Switch checked={params.json_mode ?? false}
          onChange={(v) => patchParams({ json_mode: v })} /></Field>
      </Space>
    </>
  )
}

const OP_DEFAULTS: Record<string, Record<string, any>> = {
  dedup: { op: 'dedup', columns: [] },
  filter: { op: 'filter', column: '', mode: 'contains', value: '' },
  rename: { op: 'rename', mapping: {} },
  drop: { op: 'drop', columns: [] },
  concat: { op: 'concat', target: '', columns: [], sep: '' },
  cast: { op: 'cast', column: '', to: 'str' },
  sample: { op: 'sample', n: 100 },
  shuffle: { op: 'shuffle' },
}
const OP_LABELS: Record<string, string> = {
  dedup: '去重', filter: '过滤', rename: '重命名', drop: '删除列',
  concat: '拼接列', cast: '类型转换', sample: '随机采样', shuffle: '打乱',
}
const LEN_MODES = ['min_len', 'max_len']

function OpFields({ op, update }: { op: Record<string, any>; update: (p: object) => void }) {
  switch (op.op) {
    case 'dedup':
    case 'drop':
      return <Select mode="tags" placeholder="列名（去重留空=全列）" style={{ width: '100%' }}
                     value={op.columns} onChange={(v) => update({ columns: v })} />
    case 'filter':
      return (
        <Space wrap>
          <Input placeholder="列名" style={{ width: 100 }} value={op.column}
                 onChange={(e) => update({ column: e.target.value })} />
          <Select style={{ width: 120 }} value={op.mode} onChange={(v) => update({ mode: v })}
                  options={[
                    { value: 'min_len', label: '最小长度' }, { value: 'max_len', label: '最大长度' },
                    { value: 'contains', label: '包含' }, { value: 'not_contains', label: '不包含' },
                    { value: 'regex', label: '正则匹配' },
                  ]} />
          {LEN_MODES.includes(op.mode)
            ? <InputNumber placeholder="长度" value={op.value} onChange={(v) => update({ value: v })} />
            : <Input placeholder="值" style={{ width: 120 }} value={op.value}
                     onChange={(e) => update({ value: e.target.value })} />}
        </Space>
      )
    case 'rename': {
      const [from, to] = Object.entries(op.mapping ?? {})[0] ?? ['', '']
      return (
        <Space>
          <Input placeholder="原列名" style={{ width: 120 }} value={from}
                 onChange={(e) => update({ mapping: { [e.target.value]: to } })} />
          →
          <Input placeholder="新列名" style={{ width: 120 }} value={to as string}
                 onChange={(e) => update({ mapping: { [from]: e.target.value } })} />
        </Space>
      )
    }
    case 'concat':
      return (
        <Space wrap>
          <Select mode="tags" placeholder="来源列" style={{ minWidth: 160 }}
                  value={op.columns} onChange={(v) => update({ columns: v })} />
          <Input placeholder="分隔符" style={{ width: 80 }} value={op.sep}
                 onChange={(e) => update({ sep: e.target.value })} />
          <Input placeholder="目标列" style={{ width: 100 }} value={op.target}
                 onChange={(e) => update({ target: e.target.value })} />
        </Space>
      )
    case 'cast':
      return (
        <Space>
          <Input placeholder="列名" style={{ width: 120 }} value={op.column}
                 onChange={(e) => update({ column: e.target.value })} />
          <Select style={{ width: 90 }} value={op.to} onChange={(v) => update({ to: v })}
                  options={['str', 'int', 'float'].map((t) => ({ value: t, label: t }))} />
        </Space>
      )
    case 'sample':
      return <InputNumber addonBefore="保留" addonAfter="条" value={op.n}
                          onChange={(v) => update({ n: v })} />
    default:
      return null
  }
}

function AutoProcessForm({ config, onChange }: FormProps) {
  const ops: Record<string, any>[] = config.operations ?? []
  const setOps = (next: Record<string, any>[]) => onChange({ ...config, operations: next })
  return (
    <>
      {ops.map((op, i) => (
        <div key={i} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, marginBottom: 8 }}>
          <Space style={{ marginBottom: 8 }}>
            <Select style={{ width: 130 }} value={op.op}
                    onChange={(v) => setOps(ops.map((o, j) => (j === i ? { ...OP_DEFAULTS[v] } : o)))}
                    options={Object.entries(OP_LABELS).map(([v, l]) => ({ value: v, label: l }))} />
            <a onClick={() => setOps(ops.filter((_, j) => j !== i))}>删除</a>
          </Space>
          <OpFields op={op} update={(p) => setOps(ops.map((o, j) => (j === i ? { ...o, ...p } : o)))} />
        </div>
      ))}
      <Button block onClick={() => setOps([...ops, { ...OP_DEFAULTS.dedup }])}>+ 添加操作</Button>
    </>
  )
}

function OutputNodeForm({ config, onChange }: FormProps) {
  return (
    <>
      <Field label="同时保存为新数据集">
        <Switch checked={config.save_as_dataset ?? false}
                onChange={(v) => onChange({ ...config, save_as_dataset: v })} />
      </Field>
      {config.save_as_dataset && (
        <Field label="数据集名称">
          <Input value={config.dataset_name ?? ''}
                 onChange={(e) => onChange({ ...config, dataset_name: e.target.value })} />
        </Field>
      )}
      <div style={{ color: '#999' }}>导出文件在运行详情页选择格式下载。</div>
    </>
  )
}

export default function NodeConfigForm({ type, config, onChange }: FormProps & { type: string }) {
  switch (type) {
    case 'input':
      return <InputNodeForm config={config} onChange={onChange} />
    case 'llm_synth':
      return <LlmSynthForm config={config} onChange={onChange} />
    case 'auto_process':
      return <AutoProcessForm config={config} onChange={onChange} />
    case 'output':
      return <OutputNodeForm config={config} onChange={onChange} />
    default:
      return null
  }
}
```

- [ ] **Step 7: 实现 `frontend/src/pages/WorkflowsPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Space, Table } from 'antd'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { WorkflowSummary } from '../api/types'

export default function WorkflowsPage() {
  const [list, setList] = useState<WorkflowSummary[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')

  const reload = () => api.get<WorkflowSummary[]>('/api/workflows').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  const create = async () => {
    if (!name.trim()) return
    await api.post('/api/workflows', { name: name.trim() })
    setCreating(false)
    setName('')
    await reload()
  }

  return (
    <>
      <Button type="primary" onClick={() => setCreating(true)} style={{ marginBottom: 16 }}>
        新建工作流
      </Button>
      <Table
        rowKey="id"
        dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: (v, wf) => <Link to={`/workflows/${wf.id}/canvas`}>{v}</Link> },
          { title: '更新时间', dataIndex: 'updated_at' },
          {
            title: '操作',
            render: (_, wf) => (
              <Space>
                <Link to={`/workflows/${wf.id}/canvas`}>编辑</Link>
                <Link to={`/runs?workflow_id=${wf.id}`}>运行记录</Link>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/workflows/${wf.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal title="新建工作流" open={creating} onOk={() => void create()} onCancel={() => setCreating(false)}>
        <Input placeholder="工作流名称" value={name} onChange={(e) => setName(e.target.value)} onPressEnter={() => void create()} />
      </Modal>
    </>
  )
}
```

- [ ] **Step 8: 实现 `frontend/src/pages/CanvasPage.tsx`**

```tsx
import { useCallback, useEffect, useState } from 'react'
import { Button, Drawer, Space, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Background, Controls, ReactFlow, ReactFlowProvider, addEdge,
  useEdgesState, useNodesState, type Connection, type Edge, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api } from '../api/client'
import type { Workflow } from '../api/types'
import NodeConfigForm from '../canvas/forms/NodeConfigForm'
import { nodeTypes } from '../canvas/nodeTypes'
import { NODE_LABELS, fromFlow, toFlow } from '../canvas/serialize'

function nextId(type: string, existing: Node[]): string {
  for (let i = 1; ; i++) {
    const id = `${type}_${i}`
    if (!existing.some((n) => n.id === id)) return id
  }
}

function Canvas() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [wf, setWf] = useState<Workflow | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = nodes.find((n) => n.id === selectedId) ?? null

  useEffect(() => {
    void api.get<Workflow>(`/api/workflows/${id}`).then((w) => {
      setWf(w)
      const f = toFlow(w.graph)
      setNodes(f.nodes)
      setEdges(f.edges)
    })
  }, [id, setNodes, setEdges])

  const onConnect = useCallback(
    (c: Connection) => setEdges((eds) => addEdge({ ...c, data: { kind: 'normal' } }, eds)),
    [setEdges],
  )

  const addNode = (type: keyof typeof NODE_LABELS) =>
    setNodes((ns) => [...ns, {
      id: nextId(type, ns), type,
      position: { x: 80 + ns.length * 50, y: 80 + ns.length * 40 },
      data: { config: {} },
    }])

  const save = async () => {
    await api.put(`/api/workflows/${id}`, { graph: fromFlow(nodes, edges) })
    message.success('已保存')
  }

  const run = async () => {
    await save()
    try {
      const r = await api.post<{ id: number }>('/api/runs', { workflow_id: Number(id) })
      navigate(`/runs/${r.id}`)
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  const updateConfig = (config: Record<string, any>) =>
    setNodes((ns) => ns.map((n) => (n.id === selectedId ? { ...n, data: { config } } : n)))

  return (
    <div style={{ height: 'calc(100vh - 48px)' }}>
      <Space style={{ marginBottom: 8 }}>
        <b>{wf?.name}</b>
        {(Object.keys(NODE_LABELS) as (keyof typeof NODE_LABELS)[]).map((t) => (
          <Button key={t} size="small" onClick={() => addNode(t)}>+ {NODE_LABELS[t]}</Button>
        ))}
        <Button size="small" type="primary" onClick={() => void save()}>保存</Button>
        <Button size="small" type="primary" danger onClick={() => void run()}>运行</Button>
      </Space>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
        onNodeClick={(_, n) => setSelectedId(n.id)} onPaneClick={() => setSelectedId(null)}
        fitView deleteKeyCode={['Backspace', 'Delete']}
      >
        <Background />
        <Controls />
      </ReactFlow>
      <Drawer
        title={selected ? `${NODE_LABELS[selected.type as keyof typeof NODE_LABELS]}（${selected.id}）` : ''}
        open={!!selected} onClose={() => setSelectedId(null)} width={440} mask={false}
      >
        {selected && (
          <NodeConfigForm
            type={selected.type!}
            config={(selected.data as { config: Record<string, any> }).config}
            onChange={updateConfig}
          />
        )}
      </Drawer>
    </div>
  )
}

export default function CanvasPage() {
  return (
    <ReactFlowProvider>
      <Canvas />
    </ReactFlowProvider>
  )
}
```

- [ ] **Step 9: 手动验证**

`npm run dev` + 后端：新建工作流 → 画布添加 输入/LLM合成/输出 三个节点并连线 → 点节点配置（输入选数据集、合成选模型填 prompt）→ 保存 → 刷新页面配置还在。

- [ ] **Step 10: 运行前端测试并提交**

Run: `npm test`
Expected: 全部通过

```bash
git add frontend/
git commit -m "feat: 工作流列表与画布编排"
```

---

### Task 16: 运行列表/详情页 + 生产静态托管 + README

**Files:**
- Replace: `frontend/src/pages/RunsPage.tsx`
- Replace: `frontend/src/pages/RunDetailPage.tsx`
- Modify: `backend/app/main.py`（静态托管，最终形态见下）
- Create: `README.md`

- [ ] **Step 1: 实现 `frontend/src/pages/RunsPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { Table, Tag } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { Run } from '../api/types'

export const STATUS_COLORS: Record<string, string> = {
  queued: 'default', running: 'processing', completed: 'success',
  failed: 'error', cancelled: 'warning',
}
export const STATUS_LABELS: Record<string, string> = {
  queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
}

export default function RunsPage() {
  const [list, setList] = useState<Run[]>([])
  const [params] = useSearchParams()
  const wfId = params.get('workflow_id')

  useEffect(() => {
    void api.get<Run[]>(`/api/runs${wfId ? `?workflow_id=${wfId}` : ''}`).then(setList)
  }, [wfId])

  return (
    <Table
      rowKey="id"
      dataSource={list}
      columns={[
        { title: 'ID', dataIndex: 'id', render: (v) => <Link to={`/runs/${v}`}>#{v}</Link> },
        { title: '工作流', dataIndex: 'workflow_name' },
        { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={STATUS_COLORS[s]}>{STATUS_LABELS[s] ?? s}</Tag> },
        { title: 'Token 用量', dataIndex: 'stats', render: (s: Run['stats']) => (s.prompt_tokens ?? 0) + (s.completion_tokens ?? 0) },
        { title: '创建时间', dataIndex: 'created_at' },
        { title: '结束时间', dataIndex: 'finished_at' },
      ]}
    />
  )
}
```

- [ ] **Step 2: 实现 `frontend/src/pages/RunDetailPage.tsx`**

```tsx
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Popconfirm, Progress, Select, Space, Table, Tabs, Tag, message } from 'antd'
import { useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { RowsPage, RunDetail } from '../api/types'
import { NODE_LABELS } from '../canvas/serialize'
import { STATUS_COLORS, STATUS_LABELS } from './RunsPage'

const ACTIVE = ['queued', 'running']

export default function RunDetailPage() {
  const { id } = useParams()
  const [run, setRun] = useState<RunDetail | null>(null)
  const [selectedNode, setSelectedNode] = useState<string>()
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<RowsPage>({ total: 0, rows: [] })
  const [failedPage, setFailedPage] = useState(1)
  const [failed, setFailed] = useState<RowsPage>({ total: 0, rows: [] })
  const [format, setFormat] = useState('jsonl')

  const refresh = useCallback(() => api.get<RunDetail>(`/api/runs/${id}`).then(setRun), [id])
  useEffect(() => {
    void refresh()
  }, [refresh])
  useEffect(() => {  // 运行中每 2 秒轮询
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => void refresh(), 2000)
    return () => clearInterval(t)
  }, [run?.status, refresh])

  const node = selectedNode ?? run?.graph.nodes.find((n) => n.type === 'output')?.id
  const isActive = run ? ACTIVE.includes(run.status) : true

  useEffect(() => {
    if (!run || isActive || !node) return
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&page=${page}&page_size=20`).then(setRows)
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&status=failed&page=${failedPage}&page_size=20`).then(setFailed)
  }, [run?.status, node, page, failedPage, id, isActive])

  const nodeLabel = useCallback((nid: string) => {
    const n = run?.graph.nodes.find((g) => g.id === nid)
    return n ? `${NODE_LABELS[n.type]}（${nid}）` : nid
  }, [run])

  const orderedStates = useMemo(() => {
    if (!run) return []
    const byId = Object.fromEntries(run.node_states.map((s) => [s.node_id, s]))
    return run.graph.nodes.map((n) => byId[n.id]).filter(Boolean)
  }, [run])

  if (!run) return null
  const hasFailed = run.node_states.some((s) => s.failed > 0)
  const previewColumns = Object.keys(rows.rows[0] ?? {}).map((c) => ({
    title: c, dataIndex: c, ellipsis: true,
    render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')),
  }))

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <h3 style={{ margin: 0 }}>运行 #{run.id}（{run.workflow_name}）</h3>
        <Tag color={STATUS_COLORS[run.status]}>{STATUS_LABELS[run.status] ?? run.status}</Tag>
        <span>Token：{(run.stats.prompt_tokens ?? 0) + (run.stats.completion_tokens ?? 0)}</span>
        {isActive && (
          <Popconfirm title="确认取消？" onConfirm={async () => { await api.post(`/api/runs/${id}/cancel`); message.success('已请求取消'); await refresh() }}>
            <Button danger size="small">取消</Button>
          </Popconfirm>
        )}
        {!isActive && hasFailed && (
          <Button size="small" type="primary"
                  onClick={async () => { await api.post(`/api/runs/${id}/rerun-failed`); message.success('失败行已重新入队'); await refresh() }}>
            重跑失败行
          </Button>
        )}
      </Space>
      {run.error && <Alert type="error" message={run.error} style={{ marginBottom: 16 }} />}
      <Space wrap style={{ marginBottom: 16 }}>
        {orderedStates.map((s) => (
          <Card key={s.node_id} size="small" style={{ width: 230 }}>
            <div>{nodeLabel(s.node_id)}</div>
            <Progress
              percent={s.total ? Math.round((s.done / s.total) * 100) : 0}
              status={s.failed > 0 ? 'exception' : s.status === 'done' ? 'success' : 'active'}
            />
            <div>
              {s.done}/{s.total}
              {s.failed > 0 && <span style={{ color: '#ff4d4f' }}>（失败 {s.failed}）</span>}
            </div>
          </Card>
        ))}
      </Space>
      {!isActive && (
        <>
          <Space style={{ marginBottom: 8 }}>
            查看节点：
            <Select style={{ width: 260 }} value={node}
                    onChange={(v) => { setSelectedNode(v); setPage(1); setFailedPage(1) }}
                    options={run.graph.nodes.map((n) => ({ value: n.id, label: nodeLabel(n.id) }))} />
            <Select style={{ width: 100 }} value={format} onChange={setFormat}
                    options={['jsonl', 'csv', 'xlsx'].map((f) => ({ value: f, label: f }))} />
            <Button onClick={() => window.open(`/api/runs/${id}/export?format=${format}&node_id=${node}`)}>
              导出
            </Button>
          </Space>
          <Tabs
            items={[
              {
                key: 'preview', label: `结果预览（${rows.total} 单元）`,
                children: <Table rowKey={(_, i) => String(i)} dataSource={rows.rows} columns={previewColumns}
                                 pagination={{ current: page, pageSize: 20, total: rows.total, onChange: setPage }}
                                 scroll={{ x: 'max-content' }} size="small" />,
              },
              {
                key: 'failed', label: `失败行（${failed.total}）`,
                children: <Table rowKey="row_idx" dataSource={failed.rows}
                                 columns={[
                                   { title: '行号', dataIndex: 'row_idx', width: 80 },
                                   { title: '尝试次数', dataIndex: 'attempt', width: 90 },
                                   { title: '错误', dataIndex: 'error' },
                                 ]}
                                 pagination={{ current: failedPage, pageSize: 20, total: failed.total, onChange: setFailedPage }}
                                 size="small" />,
              },
            ]}
          />
        </>
      )}
    </>
  )
}
```

- [ ] **Step 3: `backend/app/main.py` 最终形态（追加静态托管）**

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import get_session_factory, init_db
from app.engine.manager import resume_unfinished
from app.routers import auth, datasets, model_configs, runs, workflows

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await resume_unfinished(get_session_factory())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GraphFlow", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(model_configs.router)
    app.include_router(datasets.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)

    if STATIC_DIR.exists():  # 生产：托管前端构建产物，SPA 路由回退 index.html
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            file = STATIC_DIR / full_path
            if full_path and file.is_file():
                return FileResponse(file)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
```

- [ ] **Step 4: 写 `README.md`**

````markdown
# GraphFlow

面向大模型训练数据合成的可视化跑数平台：画布拖拽编排「输入 → LLM 合成 → 自动处理 → 输出」管道，后台并发执行、断点续跑、失败行重跑、结果导出。

## 开发（Windows / macOS / Linux）

后端（终端 1）：

```bash
cd backend
uv sync
uv run fastapi dev app/main.py        # http://127.0.0.1:8000，API 文档 /docs
```

前端（终端 2）：

```bash
cd frontend
npm install
npm run dev                            # http://127.0.0.1:5173，/api 已代理到后端
```

## 测试

```bash
cd backend && uv run pytest            # 后端
cd frontend && npm test                # 前端
```

## 生产部署（Linux，单进程）

```bash
cd frontend && npm install && npm run build    # 产物输出到 backend/static
cd ../backend && uv sync
export GRAPHFLOW_SECRET_KEY=<随机长字符串>      # 必须修改，用于会话签名与 api_key 加密
export GRAPHFLOW_DATA_DIR=/var/lib/graphflow   # 数据目录（SQLite/上传/导出）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://<host>:8000` 即可使用（开发模式登录：输入用户名直接进入）。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `GRAPHFLOW_DATA_DIR` | `data` | 数据落盘目录 |
| `GRAPHFLOW_SECRET_KEY` | `dev-secret-change-me` | 会话签名 + api_key 加密密钥，生产必改 |
````

- [ ] **Step 5: 整体冒烟验证（端到端）**

```bash
cd frontend && npm run build
cd ../backend && uv run uvicorn app.main:app --port 8000
```

浏览器打开 `http://127.0.0.1:8000`：登录 → 配置一个真实可用的模型（或继续用假地址验证失败路径）→ 上传 jsonl → 建工作流连线配置 → 运行 → 进度条推进 → 查看结果预览/失败行 → 导出 jsonl。**重启后端进程**验证运行中任务自动恢复、已完成行不重跑（看日志/Token 不再增长）。

- [ ] **Step 6: 全量测试 + 提交**

```bash
cd backend && uv run pytest
cd ../frontend && npm test
git add -A
git commit -m "feat: 运行页面、生产静态托管与 README——P1 完成"
```

---

## 任务依赖与执行顺序

```
T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12（后端完成）
                                                      ↓
T13 → T14 → T15 → T16（前端依赖后端 API 联调）
```

严格按编号顺序执行即可；T13 起需要后端可运行（`uv run fastapi dev app/main.py`）。

## P1 完成标准

- [ ] 全部后端测试通过（`uv run pytest`，约 45 用例）
- [ ] 全部前端测试通过（`npm test`）
- [ ] Task 16 Step 5 端到端冒烟全流程走通（含重启恢复验证）
- [ ] 两个用户登录互相看不到对方的数据集/工作流/运行

## 后续计划（不在本计划内）

- **P2**：质检节点 + 回扫环（rescan 边、`_qc_reason`/`_qc_round`、轮次上限）、LLM 写代码节点（生成/试跑/修复）、暂停/恢复、引入 Alembic（以 P1 schema 为基线）
- **P3**：mermaid DSL 双向、自然语言生成工作流、工作流导入导出、SSO 接入

