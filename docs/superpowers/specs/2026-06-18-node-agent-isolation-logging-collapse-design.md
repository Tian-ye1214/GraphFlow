# Batch 20 设计：节点助手独立化 + 思考硬编码 + 模型日志 + 折叠布局

> 日期：2026-06-18　分支：batch20-node-agent-isolation

## 目标

让每个工作流节点拥有**独立绑定、可多轮对话**的「节点助手」（会话/草稿互不共享、可并发、切节点/换页不丢）；把**整个 RedLotus + 节点助手**的思考硬编码为 `xhigh`；在**模型/Agent 网关出口**统一记录"请求↔响应"对话日志（多处可看）；并把节点配置面板重做为**全分组、默认全折叠**的折叠布局。

## 背景（现状要点）

- 批 19 已让每节点 `config.params` 带 `thinking_enabled / reasoning_effort`，两条网关（`app/services/llm.py:chat` 与 `app/agent/factory.py:create_model`）都已注入思考 → **"每节点独立配置思考"已具备**。
- **节点助手现状**：`POST /api/agent/node-assist`（`app/routers/agent.py`）→ `app/agent/codegen.py:generate_node_config`，用 `create_agent` 跑**一次性、无状态** `agent.run`，无历史、无草稿、无并发会话概念。前端 `NodeConfigForm.tsx` 内 `NodeAssist` 组件的 `instruction` 是组件本地 state，**切节点即丢**。
- **两条网关**：节点路径 `llm.chat`（裸 `openai.AsyncOpenAI`，用于 `llm_synth/qc`）；Agent 路径 `factory.create_model` 产出的 pydantic-ai Model（用于 coordinator/manager/worker/compactor/codegen/节点助手）。两者用**不同客户端**，无单一 HTTP 切口。
- **现有日志**：`RunLog`（时间线文本）、`RunRow`（token）、`AgentMessage`（会话历史）——都不是"模型请求↔响应原文"。
- **持久化**：SQLAlchemy2 async + SQLite(WAL)，**无迁移**（`Base.metadata.create_all` + `_migrate_sqlite_schema` 仅补列）；级联删除全部**手动**（见 `delete_workflow` / `delete_session`）。
- **每用户隔离**：靠路由里 `resource.user_id == user.id` 校验（无行级安全），是硬红线。

## 五块设计

### Part 1 — 思考硬编码（xhigh）

**规则**：凡 RedLotus + 节点助手（= 所有 Agent 路径 agent + compactor）一律 `thinking_enabled=true, reasoning_effort="xhigh"`，**忽略任何传入/存储的思考参数**；节点路径（`llm.chat` 的 `llm_synth/qc`）保持每节点可配（批 19 行为不变）。

- **唯一真源 `force_xhigh`**：在 `app/thinking.py` 增加 `force_xhigh(params: dict | None) -> dict`，返回 `{**(params or {}), "thinking_enabled": True, "reasoning_effort": "xhigh"}`（覆盖思考两键，保留其余如 temperature）。批 19 既有的注入逻辑（`create_model` / `llm.chat` 读 `thinking_enabled/reasoning_effort`）原样消费，无需改注入本身。
- **两个强制点**（因 RedLotus 成员跨两条路径）：
  1. **`app/agent/factory.py:create_model`**：进函数即 `params = force_xhigh(params)`，再走原思考注入 → 覆盖 coordinator/manager/worker/codegen/节点助手（全部 `agent.run` 系，含带工具的 codegen）。
  2. **`app/agent/compactor.py:_default_summarize`**（compactor 走 `llm.chat`、不经 `create_model`，是唯一漏网的 RedLotus 成员）：`llm.chat(..., params=force_xhigh(params))`。
- 节点路径（`run_llm_synth_row`/`run_qc_judge_row` 的 `llm.chat`）与连通测试（`model_configs.py` 的 ping）**不**强制，保持现状。
- **前端**：移除 `AgentDrawer` 的分角色思考控件（`ThinkingControls`），改为只读文案"思考：xhigh（固定）"；`AgentSession.model_params_json` 里残留的思考字段不再被读取（不删列，无迁移）。节点助手区域同样不显示思考开关。
- **受影响的批 19 测试**（预期 red→green 改动）：`test_agent_factory.py::test_create_model_no_key`（断言 extra_body 思考为 high → 改为 xhigh）、`test_create_model_thinking_disabled`（思考曾可关 → 现恒开，该用例改为断言"即便传 `thinking_enabled:false` 仍强制 xhigh"）。

### Part 2 + 3 — 节点助手：多轮对话 + 独立会话 + 草稿

**前端（状态层）**：在应用顶层（`App.tsx`，路由/画布之上）放一个**不随节点抽屉卸载**的助手 store。

- 形态：`Map<nodeKey, { messages: AssistMsg[]; draft: string; pending: boolean }>`，`nodeKey = `${workflowId}:${nodeId}``。
- 实现：用一个模块级外部 store + `useSyncExternalStore` 订阅（**不引入 zustand**，零新依赖），或等价的 App 根 Context。关键是 store 生命周期 = 应用级，组件卸载不清空。
- `AssistMsg = { role: 'user'|'assistant'; text: string; config?: NodeConfig }`（`config` 存在时该条助手消息下渲染「应用到节点」按钮）。
- **草稿**：节点助手输入框双向绑定 `store[nodeKey].draft`；切节点/换页时因 store 在应用级而不丢；**点「发送」即清空 draft**（内容转成一条 user message）。
- **并发/后台继续**：发送动作走 store 的 `send(nodeKey, ...)`，内部 `fetch` 的 promise 由 store 持有；即便用户切走、`NodeAssist` 组件卸载，promise resolve 时仍把助手回复写回 `store[nodeKey]`，回到该节点即见。各 `nodeKey` 互相独立、可同时在途 → 满足"会话独立、独立生命周期、同时异步"。

**后端（无状态多轮）**：改造 `POST /api/agent/node-assist`。

- 请求体新增 `history: [{role, text}]`（该节点已有对话），保留 `node_id/node_type/instruction/model_config_id/current_config` 等。
- 处理：`create_agent`（→ `create_model` 自动 xhigh）+ 把 `history` 转为 pydantic-ai `message_history`，跑一轮 `agent.run(instruction, message_history=...)`，返回 `{ reply: str, config?: NodeConfig }`（沿用现有"从输出里解析配置 JSON"的逻辑，解析到就带 `config`）。
- **无状态**：后端不存任何会话/草稿；隔离与并发由"前端各 nodeKey 独立 + 后端每请求独立"天然保证。**不新增会话/草稿 DB 表**。
- 思考：节点助手经 `create_model` → 自动 xhigh（Part 1）。

### Part 4 — 模型日志（双网关单切口 + loguru 封装 + DB 真源）

**真源 = DB 表**（供 UI 查询/筛选/用户隔离/级联删除）；**loguru 封装采集**（同一切口里额外写一份结构化 JSONL 滚动文件，给运维/排障）。

- **新表 `ModelCallLog`**（`app/models.py`）：
  `id`、`user_id`(FK, indexed)、`run_id`(FK, nullable, indexed)、`workflow_id`(FK, nullable, indexed)、`session_id`(FK, nullable, indexed)、`node_id`(str, default "")、`source`(str：`synth`/`qc`/`coordinator`/`manager`/`worker`/`compactor`/`codegen`/`assistant`)、`model_config_id`(nullable)、`model_name`(str)、`provider`(str)、`request_json`(text)、`response_json`(text)、`prompt_tokens`(int)、`completion_tokens`(int)、`created_at`。
- **单切口函数 `log_model_call(...)`**（新文件 `app/services/model_log.py`）：
  - 入参含上述上下文 + 已脱敏的 messages + 响应文本 + usage。
  - 🔒 **铁律**：`request_json` 只存 messages（system/user/历史），**绝不**写 `api_key`/`Authorization` 头/任何凭据。
  - **节点类限量**（仅 `source ∈ {synth, qc}`）：**失败行全留 + 成功行前 N=20 条/(run_id,node_id)**。计数用一个进程内 `(run_id,node_id)→成功计数` 表（run 结束/删除即弃）；失败（`llm.chat` 重试耗尽抛错那条）无条件记。其余 `source`（Agent 类）**全量**。
  - 持久化到 `ModelCallLog`，并 `logger.bind(source=…, run_id=…, node_id=…).info(...)` 写 JSONL。
- **网关 A（`llm.chat`，覆盖 synth/qc/compactor）**：在 `app/services/llm.py:chat` 内，`client.chat.completions.create` 成功后、以及重试耗尽抛 `LLMError` 前，调用 `log_model_call(...)`。`chat()` 增加可选 `log_ctx={run_id,node_id,source,user_id,...}`，无 `log_ctx` 时不记。三个透传点：
  - `run_llm_synth_row`（`source='synth'`）、`run_qc_judge_row`（`source='qc'`）→ 节点类**限量**；
  - `compactor._default_summarize`（`source='compactor'`，`user_id` 取自 `compactor_mc.user_id`，`session_id` 尽力而为）→ Agent 类**全量**；
  - `model_configs.py` 的连通测试 ping **不传 `log_ctx`** → 不记。
- **网关 B（Agent 路径）**：新增 `LoggingModel`（`app/agent/logging_model.py`），用 **loguru 封装**——它实现 pydantic-ai Model 接口（`request` / `request_stream`），透传给被包裹的真实 Model，在返回后用响应的 messages/usage 调 `log_model_call(...)`。`factory.create_model` 在返回前把产物用 `LoggingModel` 包一层 → 所有 agent 自动经此单切口、Agent 类全量。上下文（user_id/session_id/source 等）通过 `create_model`/`create_agent` 透传给 `LoggingModel`。
- **查看（多处）**：
  - 全局「模型日志」页：`GET /api/model-logs?run_id=&node_id=&source=&limit=&offset=`（强制 `user_id==当前用户`），前端新增页面 + 导航入口，按 run/节点/source 筛选，点开看某条请求↔响应原文。
  - run 详情页新增「模型对话」Tab：`GET /api/runs/{run_id}/model-logs`（复用隔离 + run 归属校验）。
- **级联删除**（手动，沿用现有模式）：`delete_run`/`delete_workflow`/`delete_session` 里按 `run_id`/`workflow_id`/`session_id` 一并删 `ModelCallLog`。新表加进 `models.py` 即随 `create_all` 自动建。

### Part 5 — 折叠布局

- `NodeConfigForm.tsx` 各子表单改用 AntD `Collapse`：**全分组、默认全部折叠**（`defaultActiveKey=[]`），可多开（非手风琴）。
- 分组（以 `llm_synth` 为例）：**模型** / **提示词** / **高级**（思考·温度·输出·列）/ **RedLotus 助手**。各节点类型按自身字段套同样分组（如 `qc`、`http_fetch`、`auto_process`、`input`、`output`）。
- 思考相关：节点路径节点（`llm_synth/qc`）的思考控件仍在「高级」组内可配；Agent 侧（AgentDrawer）按 Part 1 移除。

## 数据流（关键路径）

1. **配置节点助手**：用户在某节点输入框打字 → 实时写 `store[nodeKey].draft`（切节点不丢）→ 点发送 → `store.send` 清 draft、追加 user message、`fetch /api/agent/node-assist`（带 history）→ 后端 xhigh agent 跑一轮 → 返回 `{reply, config?}` → store 追加 assistant message（component 卸载也回填）→ 有 config 则消息下出「应用到节点」→ 点了才改 `node.config`。
2. **运行时合成**：`runner` → `run_llm_synth_row`/`run_qc_judge_row` → `llm.chat(..., log_ctx={run_id,node_id,source})` → 网关 A 记日志（节点类限量）。
3. **任意 Agent 调用**：`create_agent` → `create_model`(xhigh + 包 `LoggingModel`) → `agent.run` → 网关 B 记日志（Agent 类全量）。

## 错误处理

- 日志写入失败不得影响主流程：`log_model_call` 内部 try/except 吞异常并 `logger.warning`，绝不让"记日志"把一次正常的模型调用搞失败。
- 节点类限量计数器是进程内、尽力而为；进程重启计数归零（可接受，KISS）。
- 后端无状态多轮：`history` 由前端给，后端不校验其完整性（信任同源前端），但仍走 `model_config` 归属校验与 user 隔离。

## 测试策略（TDD）

**后端**
- `force_xhigh`：任意输入（含 `thinking_enabled:false`）→ 两键被覆盖为 `True/"xhigh"`，其余键保留。
- `create_model`：任意 `default_params_json`（含 `thinking_enabled:false`）→ 产物思考恒为 xhigh-enabled；更新批 19 两个用例。
- compactor：`_default_summarize` 调 `llm.chat` 时 `params` 思考被强制 xhigh（监视传入 `llm.chat` 的 params）。
- node-assist 多轮：传 `history` → 断言以 `message_history` 调用 agent；返回 `{reply, config?}`；经 xhigh。
- `log_model_call` 限量：同 `(run,node)` 成功第 1..20 条记、第 21 条不记；失败条无条件记；Agent 类（`source` 非 synth/qc，含 `compactor`）全量。
- **脱敏断言**：`request_json` 不含 api_key/Authorization（构造带密钥的 mc，断言落库记录里无凭据）。
- 网关 A/B：`llm.chat` 带 `log_ctx` 落一条；`LoggingModel` 包裹后 `agent.run` 落一条。
- 查询端点：`GET /api/model-logs` 与 `/api/runs/{id}/model-logs` 的 user 隔离（他人 run/log 404 或空）+ 筛选。
- 级联：删 run/workflow/session → 对应 `ModelCallLog` 一并删。

**前端**
- `npm run build` 绿（tsc 干净）。
- 助手 store：不同 `nodeKey` 的 draft/messages 互不串；发送清 draft；卸载后回填（若有测试基建，否则手验关键路径）。

## 非目标（YAGNI）

- 不做会话/草稿的跨设备/跨刷新持久化（用户已确认 F5 丢可接受）。
- 不做"多个节点助手面板并排同时可见"（用户选后台继续 + 各自独立）。
- 不改 `llm_synth/qc` 节点的运行语义，只加日志 `log_ctx`。
- 不引入 zustand 或其它新前端状态库。

## 待 review 确认点

1. loguru 定位：**DB 表为 UI 真源，loguru 仅作网关采集封装 + JSONL 排障文件**——是否就按此双写？（若你想 loguru 文件作唯一存储、UI 直接读文件，告诉我，会改 Part 4 架构。）
2. Part 1 强制 xhigh 落在 `create_model` 单切口（覆盖一切 Agent，含 codegen）——`codegen` 也一并 xhigh，可否？
