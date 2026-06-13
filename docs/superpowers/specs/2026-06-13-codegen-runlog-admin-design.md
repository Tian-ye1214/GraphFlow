# 设计：自动处理增强 + 运行日志/删除 + admin 租户管理

日期：2026-06-13　分支：`feature/codegen-runlog-admin`

承接 `agent-fixes` 批次（已合并 master@575de58）。用户四项需求，均经 AskUserQuestion 收束方案。

KISS 硬规则全程有效：最简实现、无投机抽象、不预防未发生的 bug。api_key 经 Fernet 加密，**绝不**出现在任何响应/日志/Agent 提示中。每用户资源隔离是硬验收红线。

---

## A. 自动处理节点：提升生成质量 + 让 Agent 代码更显眼

**现状**：`auto_process` 节点已支持 `{"op": "agent", "code": ...}`——自然语言指令 → `/api/agent/codegen` 生成 Python → `pycode.run_process_code` 子进程执行；内置算子（dedup/filter/…）保留。能力已存在，痛点在生成质量与可发现性。

### A1 生成质量
`backend/app/agent/codegen.py` 的 `INSTRUCTIONS` 增强：
- 明确告知运行环境可 `import pandas as pd`（pandas 已是依赖）。
- 内嵌 2–3 个精简 few-shot 示例，**必须含「按列分组后组内去重」**（用户痛点：先按 session 分组再去重），另含全局去重、过滤。
- 强调 `process(rows: list[dict]) -> list[dict]` 保持行字典结构、数据问题自然报错（保留现有硬性要求）。

不改 `generate_with_repair` 的试跑-修复循环。

### A2 更显眼
`frontend/src/canvas/forms/NodeConfigForm.tsx` 的 `AutoProcessForm`：
- 在「+ 添加操作」旁加醒目主按钮「✨ 用 AI 写处理代码」，点击直接追加一个 `agent` 操作（`{ ...OP_DEFAULTS.agent }`）。
- 一行灰字提示：复杂处理（分组去重等）建议用 AI 写代码。

### A 测试
- `backend/tests/test_pycode.py` 加一例：手写 pandas `process()` 做「按 session 分组组内去重」，经 `run_process_code` 子进程跑通并断言结果——确定性验证子进程能 import pandas、分组模式可用。
- `INSTRUCTIONS` 含分组去重指引的断言（防回退）。
- codegen 调 LLM 不可确定性单测，不强测其输出。
- 前端：`AutoProcessForm` 点新按钮后 `operations` 末尾出现 `op==='agent'` 项。

---

## B. 运行日志：节点级时间线（持久化 + 展示 + 下载）

### B1 数据模型
新表 `RunLog`（`backend/app/models.py`）：
```
id: int PK
run_id: int FK(runs.id) index
node_id: str default ""        # "" 表示运行级事件
level: str default "info"      # info / error
message: str (Text)
created_at: datetime(tz) default now
```

### B2 落日志
`backend/app/engine/runner.py` 加助手 `_log(session_factory, run_id, node_id, message, level="info")`（独立 session 插入一行）。调用点：
- `_execute` 起始：运行级「运行开始」。
- `_set_node_state` 内按状态集中落（DRY）：
  - `running` → `▶ 节点 {node_id} 开始`
  - `done` → `✓ 节点 {node_id} 完成（done={done} failed={failed}）`
  - `failed` → `✗ 节点 {node_id} 失败（failed={failed}）`，level=error
- `_finish` → 运行级「运行结束：{status}（prompt={p} completion={c}）」。
- `execute_run` 异常处理 → 运行级「运行失败：{e}」，level=error。

### B3 端点
`GET /api/runs/{run_id}/logs`（`_get_owned_run` 归属校验）→ 按 `id` 升序返回 `[{created_at, node_id, level, message}]`。

### B4 前端
`frontend/src/pages/RunDetailPage.tsx` 加「运行日志」面板（antd `Timeline` 或滚动列表），随 SSE `run` 事件刷新。
**下载**：前端把已取日志格式化为文本、客户端 `Blob` 下载（`run{id}.log`）——不加额外端点。

---

## C. 运行删除：单条 + 级联清理

### C1 端点
`DELETE /api/runs/{run_id}`（`backend/app/routers/runs.py`）：
- `_get_owned_run` 归属校验。
- `status in ("queued","running")` → 409「运行中，请先取消再删除」（拒绝活跃运行，避免与后台任务写库竞态）。
- 级联删除：`RunRow`、`RunNodeState`、`RunLog`（按 run_id）；`WorkflowVersion`（`run.workflow_version_id`，与 run 1:1）；导出文件 `data_dir/exports/run{run_id}_*`（glob unlink）；最后删 `Run` 本身。
- 返回 `{"ok": True}`。

### C2 前端
`RunsPage.tsx` 加「操作」列 + 删除按钮（`Popconfirm`），删后 `reload()`。

---

## D. admin 租户管理：act-as + 账号增删

### D1 is_admin + 白名单
- `User` 加 `is_admin: bool default False`。
- `Settings`（`config.py`）加 `admin_users: str = ""`（env `GRAPHFLOW_ADMIN_USERS`，逗号分隔用户名）+ 属性 `admin_user_set`（去空白的集合）。
- `DevAuthProvider.login`：登录/建号时 `user.is_admin = user.username in settings.admin_user_set` 并提交（白名单为准，env 增删下次登录生效）。

### D2 act-as（有效用户切换）
- 签名 cookie `gf_act_as`（复用 `TimestampSigner(secret_key)` 同 `make_session_cookie` 机制）存目标 user_id。
- `backend/app/auth.py`：
  - 新增 `get_real_user(...)`：仅从 `gf_session` 解析真实用户（忽略 act-as）。
  - `get_current_user` 返回**有效用户**：解析真实用户后，**仅当** `real.is_admin` 且 `gf_act_as` cookie 有效 → 加载并返回目标用户；否则返回真实用户。非管理员的 `gf_act_as` 一律忽略（即便签名有效，也守住隔离红线）。
  - 新增 `require_admin` 依赖 = `get_real_user` + 断言 `is_admin`（否则 403）。
- 全部现有归属端点无需改动（它们用 `get_current_user` 的有效用户）。

### D3 端点（`backend/app/routers/admin.py`，前缀 `/api/admin`，均 `require_admin`）
- `POST /act-as` body `{user_id: int | None}`：`None` → 删 `gf_act_as` cookie（返回管理员自身）；否则校验目标用户存在 → set 签名 cookie。返回有效用户信息。
- `GET /users` → 全部用户 `[{id, username, display_name, is_admin, created_at}]`。
- `POST /users` body `{username, display_name?}` → 建用户（is_admin 按白名单算）；用户名重复 422。
- `DELETE /users/{user_id}` → 拒绝删自己（409）；级联删该用户全部资源：
  - `Dataset`（+ `DatasetRow` + 磁盘文件）、`ModelConfig`、`Workflow`（+ `WorkflowVersion`）、`Run`（+ `RunRow`/`RunNodeState`/`RunLog`/导出文件）、`AgentSession`（+ `AgentMessage`）、Agent 工作目录 `data/agent/<用户名>/`。
  - 最后删 `User`。

### D4 /api/me 扩展
`backend/app/routers/auth.py` 的 `me`：同时注入 `get_real_user`（真实用户）与 `get_current_user`（有效用户）两个依赖。返回有效用户的 `{id, username, display_name}` + `is_admin`（真实用户的）、`real_username`（真实用户名）、`acting_as`（impersonate 时为有效用户名，否则 null，由「有效 != 真实」判定）。

### D5 前端
- `frontend/src/api/types.ts`：`UserInfo` 加 `is_admin`、`acting_as: string | null`、`real_username`。
- `frontend/src/stores/auth.ts`：加 `actAs(userId: number | null)`（调端点后 `init()` 刷新）。
- 新 `frontend/src/pages/AdminPage.tsx`（route `/admin`，仅 `is_admin` 可见）：用户表（用户名/显示名/is_admin/创建时间），每行「以此身份操作」（actAs(id)）/「删除」（Popconfirm）+「新建用户」。
- 顶栏（`App.tsx` 用户栏）：`is_admin` 时显示「管理」入口；`acting_as` 时显示横幅「正在以 \<acting_as\> 身份操作 · 返回管理员」（点击 `actAs(null)`）。

### D 安全验收
- 非管理员伪造 `gf_act_as` cookie → `get_current_user` 仍返回其本人（不切换）。
- 非管理员访问 `/api/admin/*` → 403。
- act-as 期间，所有归属端点以目标用户身份工作；删账号级联不残留资源/文件。

---

## 实现顺序（子代理驱动，约 13 任务）

后端先于前端，建表/配置先于用端点：

1. A1 codegen INSTRUCTIONS 增强 + 测试。
2. A2 前端 AutoProcessForm「用 AI 写代码」按钮 + 测试。
3. B1 RunLog 模型。
4. B2 runner `_log` 落日志（_set_node_state/_finish/_execute/execute_run）+ 测试。
5. B3 `GET /runs/{id}/logs` 端点 + 测试。
6. C1 `DELETE /runs/{id}` 级联 + 测试。
7. B4+C2 前端：RunDetailPage 日志面板+下载、RunsPage 删除列。
8. D1 User.is_admin + Settings.admin_users + login 刷新 + 测试。
9. D2 auth：get_real_user / 有效用户 get_current_user / require_admin + 测试。
10. D3 admin 路由：act-as + users CRUD + 级联删 + 测试。
11. D4 /api/me 扩展 + 测试。
12. D5 前端：types/auth store/AdminPage/顶栏横幅。
13. 全量回归（后端 pytest、前端 vitest+build）+ 收尾。

每任务：实现子代理（sonnet）→ spec 评审 → 代码质量评审 → KISS 仲裁；微修用 haiku。

## 验收
- 后端 `cd backend && uv run pytest` 全绿（新增测试覆盖 A 子进程分组、B 日志落库与端点、C 级联删除、D is_admin/act-as 隔离/账号级联删）。
- 前端 `cd frontend && npx vitest run` 全绿 + `npm run build` 通过。
- 手验：分组去重指令生成可用代码；运行详情见时间线日志并可下载；删除运行清空其行/状态/日志/导出；admin 可 act-as 某用户并以其身份增改、可建/删账号；非 admin 无法 act-as 或访问 /api/admin。
