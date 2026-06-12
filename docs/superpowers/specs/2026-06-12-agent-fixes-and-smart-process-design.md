# Agent 修复与智能处理操作 设计文档

日期：2026-06-12
状态：待用户审阅
前置：`2026-06-11-redlotus-agent-design.md`（Agent 平台已合并进 master）

## §1 背景与问题清单

用户反馈六项（编号沿用原始反馈）：

1. 页面没有登录登出入口，也不显示当前用户。
2. Agent 没有和当前用户绑定（**已定位为 bug**，见 §3）。
3. 自动处理节点要接入 Agent：根据自然语言和上游输入处理数据，Agent 自己写代码，然后输出。
4. `backend/data/agent/` 目录的用途疑问（已答复：Agent 会话工作目录，按会话 id 命名；其中的嵌套目录是 bug 2 的产物）。
5. Agent 会话要做隔离——经澄清：修复 bug 2 的用户绑定之外，每个 Agent（如代码生成 Agent）要有独立生命周期与上下文，不与聊天会话混用。
6. Agent 面板「高级」按钮被截断；补充要求：整个 Agent 面板从右侧抽屉改为**底部终端式面板**。

补充要求：README 教程要写成清晰的分步上手教程；Agent 经 gf CLI 的变动必须实时推送渲染到前端（即既有的按用户 SSE 发布订阅机制，bug 2 修复后自动恢复，无需新机制）。

## §2 登录登出与当前用户显示

- 后端：`POST /api/auth/logout`，`response.delete_cookie(COOKIE_NAME)`，返回 `{"ok": true}`。无需登录态也可调用（幂等）。
- 前端：`stores/auth.ts` 增加 `logout()`（调接口后置 `user: null`）；`App.tsx` 的 Sider 底部固定一条：当前用户 `display_name`（无则 `username`）+「退出」按钮，点击后跳 `/login`。

## §3 Agent 用户绑定修复（bug 2，根因已实证）

**根因链**：`settings.data_dir` 默认相对路径 `"data"` → `session_dir()` 返回相对路径 → `GF_STATE_FILE=data\agent\<id>\cli.json`（相对字符串）传给 gf 子进程 → 子进程 cwd 是会话目录，相对路径被二次拼接成 `data\agent\<id>\data\agent\<id>\cli.json` → 文件不存在，gf 报「未登录」→ Agent 自行执行 `gf login`，开发模式登录凭空创建幽灵用户（实际产生了用户 3）→ Agent 全部操作以幽灵用户进行：资源互不可见、SSE 事件发错队列（前端不刷新）、多个会话共享幽灵用户造成「会话不隔离」的表象。

所有既有测试均使用绝对临时路径（tmp_path），故未暴露；仅生产默认配置（相对 `data`）触发。

**修复**：

- `turns.session_dir()` 返回绝对路径：`(settings.data_dir / "agent" / str(session_id)).resolve()`。workdir 与 state_file 全部由它派生，一处修复全链生效（含 Worker 的 `worker_<label>_cli.json`）。
- `tools.run_command` 硬拦 `gf login`（大小写不敏感正则，与 `GF_DELETE_RE` 同款机制）：返回「会话已绑定当前用户，禁止 gf login 切换身份」。依据：该洞已被实际踩到，非投机防御。
- 回归测试：(a) `settings.data_dir` 为相对路径时 `session_dir()` 仍是绝对路径；(b) `gf login` / `GF LOGIN` 被拦截且不产生子进程。
- 一次性脏数据清理（实施计划收尾步骤，非代码）：删除 `backend/data/agent/*/data` 嵌套目录；删除 DB 中幽灵用户 3 及其名下全部资源（其 workflow 等均为 Agent 误操作产物）。

**推送机制说明（用户补充关切）**：按用户隔离的 SSE 发布订阅（`events.publish(user_id, entity, id, **extra)` → 用户队列 → 前端订阅刷新）自 P1 起就是全部写 API 的统一出口，gf CLI 走同一条链路；bug 2 修复后 Agent 的变动自动恢复实时渲染。已有 e2e 断言（Agent 跑 gf 时收到 `entity=workflow` 事件）。**不新增机制。**

## §4 智能处理操作（自动处理节点接入 Agent）

### 形态（用户已拍板）

自动处理节点操作链新增一种操作，与去重/过滤等并列、可混排：

```json
{"op": "agent", "instruction": "把 question 列翻译成英文列名 q_en，删掉空行", "code": "<固化的 Python 代码>"}
```

代码在**配置时生成并固化**（用户拍板），运行时只执行固化代码：快、可重现、可审计。改指令后需重新生成。

### 配置时生成：`POST /api/agent/codegen`

请求：`{workflow_id, node_id, instruction, model_config_id}`（均必填；校验工作流与模型归属当前用户）。

流程：

1. **取样本行**（给 Agent 看数据形状 + 试跑用），按优先级：
   a. 该工作流最近一次运行中，此节点上游的输出前 5 行（`RunRow` 已持久化，最准确）；
   b. 无运行记录 → 沿图上溯到 input 节点，取其数据集前 5 行（中间若有 llm_synth，新列缺失，能用但不全）；
   c. 都没有 → 无样本：仍可生成代码但跳过试跑，响应中说明。
2. **代码生成 Agent**：临时单 Agent（`factory.create_agent`，零工具、零历史、请求级生命周期），与聊天会话完全无关——满足 §1 第 5 项的上下文隔离。提示词 = 指令 + 列名 + 样本行（JSON），要求输出纯 Python 源码：必须定义 `def process(rows: list[dict]) -> list[dict]`，可 import 后端环境已有的库（pandas 等），禁止网络/文件外读写说明写入提示词（行为约束，不做技术沙箱，见下）。从回复中剥离 ```python 围栏。
3. **试跑 + 自动修复**：用 §4 运行时同款 harness 在样本行上执行；异常则把 traceback 回喂 Agent 修复，最多 3 轮；仍失败 → 返回 `{error, code}`（用户可改指令重试或手改代码）。
4. 成功 → 返回 `{code, preview_rows, sample_source}`。

### 运行时执行：`nodes.py` 新增 `_agent` 操作

- `op.code` 为空 → `ValueError("智能处理操作未生成代码")`，走既有节点失败链路。
- 子进程执行（不在后端进程内 `exec`——用户代码死循环/崩溃不能拖垮事件循环）：
  - 输入行写临时 JSON 文件；spawn `sys.executable <harness>`，harness 加载 op.code、调 `process(rows)`、结果 JSON 写 stdout；
  - 复用 `agent/subproc.run_subprocess`（超时杀进程树），超时固定 120 秒；
  - 退出码非 0 / stdout 非合法 JSON 数组 → 抛 ValueError 带 stderr 摘要。
- harness 为 `app/engine/` 内固定脚本，不含用户数据。
- `apply_operations` 是同步函数、`_agent` 需要子进程——`auto_process` 节点执行处（`runner._barrier_output`）按操作链顺序执行：纯函数操作走既有同步路径，`agent` 操作走异步子进程。实现上将 `apply_operations` 拆为逐操作迭代以便混排（接口细节留给实施计划）。

**安全取舍（知情决策）**：生成代码与现有 Agent `run_command` 同权限等级（受信单机场景），子进程超时防呆即可，不做更深沙箱。api_key 不进 codegen 提示词（提示词只含指令、列名、样本行）。

### 前端（AutoProcessForm 新操作 UI）

操作类型下拉新增「智能处理」：

- 指令 TextArea + 模型选择（复用用户模型配置列表）+「生成代码」按钮；
- 生成后显示**可编辑代码框**（TextArea 等宽字体，用户可手改）+ 预览结果表格（前 5 行）+ 样本来源说明；
- 代码存 `op.code`，保存画布即固化；生成中按钮 loading，失败显示错误信息。

## §5 Agent 面板改为底部终端式（含「高级」截断修复）

- `AgentDrawer` 的 `Drawer` 改 `placement="bottom"`、`height="45vh"`、全宽、`mask={false}`：像 IDE 终端一样停靠底部，不遮画布主体；FloatButton 照旧呼出。
- 标题行全宽后放下全部控件：会话选择 + 新建 + 模型 + 高级（截断问题随布局消失）；高级展开的三角色选择仍在内容区顶部。
- 消息区/输入框布局随高度调整（内容区高度 = 面板高度 − 标题 − 输入区）；工具输出维持等宽字体。不做拖拽调高（KISS）。

## §6 README 教程重写

把 README 的「开发」之后改为分步上手教程（保持一键启动/开发/生产部署/环境变量章节）：

1. **快速上手**（Web 端从零到一）：登录 → 模型配置（base_url/api_key 填法）→ 上传数据集（支持格式）→ 画布搭流水线：四种节点各自怎么配（input 选数据集；llm_synth 选模型、提示词模板 `{{列名}}`、输出方式；auto_process 各操作含新「智能处理」；output 存数据集开关）→ 连线 → 保存 → 运行 → 运行详情（进度/失败行重跑/断点续跑）→ 导出。
2. **gf CLI**：现有内容保留，补一个完整可复制的端到端示例（已有）。
3. **Agent 红莲**：现有内容基础上补「智能处理操作」用法与底部面板说明。

写作以「每步一个动作 + 预期看到什么」为标准，控制在 README 可读长度内。

## §7 测试与验收标准

1. 登出后 `/api/me` 返回 401；前端显示当前用户名（后端测试 + 构建通过）。
2. `session_dir()` 在相对 `data_dir` 下返回绝对路径；`gf login` 任意大小写被拦截。【bug 2 回归】
3. codegen 端点：FunctionModel 桩下生成→试跑→返回 code+preview；首版报错时修复循环生效；无样本时跳过试跑；跨用户 workflow/model 返回 404/422。
4. `_agent` 运行时操作：正常转换行数据；超时被杀；坏输出（非 JSON）报错；与其他操作混排顺序正确。
5. 现有全量测试零回归（后端 177 基线 + 前端 10 基线 + 构建）。
6. 人工验收：真实环境下 Agent 建工作流，画布实时出现（推送恢复）；幽灵用户与嵌套目录清理完毕。

## §8 范围外

- 不新增推送机制（既有 SSE 发布订阅已满足）。
- 不做代码执行沙箱（知情决策，见 §4）。
- 底部面板不做拖拽调高。
- 聊天 Agent（红莲会话）不参与 codegen——智能处理用独立临时 Agent。
