# 设计：把节点操作封装成结构化工具（Phase 1 — 主 Agent 的 workflow/node/edge 工具）

日期：2026-06-26
状态：已与用户在 brainstorming 中逐项确认，待 spec 复审

## 0. 背景与缘起

用户提出两件事：

1. **HTTP 取数逐条处理 + 循环** —— 经核查**已完成**（2026-06-26 master@80c48e2，`http_fetch` 节点的 `records_path` 数组展开）：`run_http_fetch_row` 配 `records_path` 时把响应数组炸成 N 行，下游逐行节点（`_run_per_row_node`）逐条处理，不是把整坨 JSON 塞给下游。`backend/tests/test_http_node.py` 37 项全过，含 `test_http_data_source_explodes_into_dataset` 等 e2e。**本设计不涉及该项。**

2. **把节点操作封装成工具** —— 本设计的主题。现状：主 RedLotus Agent 操作 GraphFlow 的唯一写入途径是 `AgentToolkit.run_command` 拼 `gf` 命令字符串跑 CLI（`backend/app/agent/tools.py:130`），靠 `gf-*` skills 教它怎么拼。用户要把这些操作做成**结构化的 pydantic-ai 工具**，让模型直接带参数调用，而非生成 shell 字符串。

## 1. 已锁定的决策（brainstorming 结论）

| 维度 | 结论 |
|---|---|
| 操作范围（最终目标） | **全生命周期**（workflow/node/edge/run/model/dataset/prompt 全部 gf 面） |
| 面向对象（最终目标） | 主 RedLotus Agent **与** 节点助手都给 |
| 与 run_command 的关系 | **工具为主**；`run_command` 保留作通用 shell（跑脚本等非 gf 命令）；gf CLI 依旧给人用 |
| 工具颗粒度 | **细粒度**：一个 gf 子命令 = 一个工具（**注意**：不是「一个 config 键一个工具」，详见 §3 注） |
| 节点助手写入作用域 | 仅当前节点 / 当前工作流 |
| 实现层 | 用 **pydantic-ai 框架包装**成工具，工具体直连服务层（与现有只读工具同范式 = 方案 A） |
| 推进方式 | **分三期**，本 spec 只覆盖 Phase 1 |

**分期划分：**
- **Phase 1（本 spec）**：`graph_ops` 单点化抽取（CLI 去重）+ 主 Agent 的 **workflow / node / edge** 结构化工具。
- **Phase 2（后续）**：主 Agent 的 **run / model / dataset / prompt** 工具（补齐全生命周期）。
- **Phase 3（后续）**：**节点助手**集成（草稿调和 + 作用域工具）。

## 2. 现状关键事实（决定复用哪一层）

- **图编辑是客户端 read-modify-write**：gf CLI 的加节点/连线/配置/op 都在 `app/cli/client.py` + `app/cli/commands/node.py·workflow.py` 里对 graph dict 本地改，再 `PUT /api/workflows/{id}` 整图替换。服务端**没有**原子的「加节点」端点。
- **现有 Agent 只读工具的范式**：`make_preview_tools / make_node_info_tools / make_catalog_tools` 都是 `(session_factory, user_id, …)` → 直连 DB + 归属校验的 pydantic-ai 工具。新写入工具沿用此范式。
- **主 Agent 工具装配点**：`AgentSystem._make_tools(state_file)`（`app/agent/system.py:44-48`）现 = `AgentToolkit.tools + SkillsToolkit.tools`，二者都已拿 `session_factory + user_id`。新工具做成第三个 toolkit 追加即可。
- **画布已防抖自动保存 + SSE 调和**：`CanvasPage.tsx` 监听 `workflow` SSE 事件，本地无改动→自动重载，有改动→提示「已被外部修改」（`cliChanged`）。**「外部改图→画布反映」是现成链路**（今天 gf-via-run_command 就在用）。
- **节点助手是草稿导向**：`POST /api/agent/node-assist` 读 `current_config`、返回 `{reply, config, sample_source}` 的 config 补丁让前端合并进表单，**不写库**。（Phase 1 不动它。）

## 3. Phase 1 工具清单（细粒度 pydantic-ai 工具）

> **§3 注（颗粒度口径）**：「一操作一工具」按**一个 gf 子命令 = 一个工具**实现。`gf node set` 本身收 `key=value` 多对，故 `set_node_config` 是**单个工具收一个 `config` dict**，而非把 ~20 个配置键各拆一个工具。已与用户确认。

### 写入工具（新建，由 `graph_ops` 支撑）

| 工具 | 对应 gf | 关键行为 |
|---|---|---|
| `create_workflow(name)` | wf add | 复用 workflow 创建 service/REST |
| `rename_workflow(workflow_id, name)` | wf rename | |
| `delete_workflow(workflow_id)` | wf rm | 级联由既有删除路径保证 |
| `add_node(workflow_id, node_type, node_id=None)` | node add | 类型校验、缺省自动生成 `{type}_{i}` id、查重、默认 position/空 config；返回 node_id |
| `remove_node(workflow_id, node_id)` | node rm | 连带删除该节点的边 |
| `connect_nodes(workflow_id, source, target, kind="normal")` | link | 校验 rescan 边必须从 qc 出发；边查重 |
| `disconnect_nodes(workflow_id, source, target)` | unlink | 不存在则报错 |
| `set_node_config(workflow_id, node_id, config: dict)` | node set | 同 §3.1 键表；dataset/model/judge_models 名→id 解析 |
| `set_node_prompt(workflow_id, node_id, slot, body=None, library_ref=None, mode="copy")` | node prompt | slot ∈ {system,user}；内联 body / 引用库(mode=ref) / 复制库正文(mode=copy) |
| `add_node_op(workflow_id, node_id, op, params)` | op add | auto_process：dedup/filter/rename/drop/concat/cast/sample/shuffle |
| `remove_node_op(workflow_id, node_id, index)` | op rm | 1-based 序号，越界报错 |

### 读取工具（复用现有 factory，Phase 1 顺手接到主 Agent，不重写）

`show_workflow_graph`、`list_workflows`、`workflow_columns`（列血缘）、`list_node_ops`。其中 `show_workflow_graph` 等已存在于 `node_info`/`catalog`（现仅接节点助手）；Phase 1 仅新增「接到主 Agent」的装配，逻辑零改动。

### Phase 1 明确不含
`restore_workflow_from_run`（涉及 run）、`wf export/import`（文件 + 多资源）→ 归 Phase 2；节点助手任何改动 → 归 Phase 3。

### 3.1 `set_node_config` 接受的键（与 `gf node set` 完全一致）

来自 `cmd_node_set`：`dataset`（→dataset_ids，名→id，逗号分隔）、`model`（→model_config_id，名→id）、`save_as`（→save_as_dataset+dataset_name）、`judge_models`（→judge_model_ids，名→id，逗号分隔）、`pass_k`/`max_rounds`（int）、`count`（int 或留空=None 不限）、HTTP 字符串键 `url/endpoint/method/body`、`extract`（`列:JSON路径` 冒号映射）、LLM 配置键 `system/prompt/out/mode/fanout/conc/retries`、LLM 采样键 `temp/top_p/max_tokens/timeout/json_mode`（落进 `params`）、`drop`（→drop_columns）、`outs`（→output_columns）、`status_col`/`feedback_col`、`think`（→params.thinking_enabled）、`effort`（→params.reasoning_effort）、`headers`（`名:值` 冒号映射）。未知键报错。

## 4. 架构：单点化 + 复用

### 4.1 新模块 `app/services/graph_ops.py`（纯函数，不碰 DB）

把今天散在 CLI 的图变更逻辑单点化。函数对 graph dict 操作，失败抛 `GraphOpError(ValueError)`：

- `find_node(graph, node_id) -> node`
- `add_node(graph, node_type, node_id=None) -> str`（返回最终 id）
- `remove_node(graph, node_id) -> None`
- `connect(graph, source, target, kind) -> None`
- `disconnect(graph, source, target) -> None`
- `apply_node_config(node, key, value) -> None`（§3.1 的键→字段映射 + 类型转换）
- `add_op(node, op, params) -> dict` / `remove_op(node, index) -> dict`

并把 `build_op`、`LLM_CONFIG_KEYS`、`LLM_PARAM_KEYS`、`HTTP_STR_KEYS`、`convert`、`_parse_colon_map`、`NODE_TYPES`、`_auto_node` 取节点逻辑等从 `cli/client.py`/`cli/commands/node.py` 迁入或共享。

**名字→id 解析不进 graph_ops（保持纯净）。** graph_ops 暴露常量 `RESOLVE_KEYS = {"dataset": "datasets", "model": "models", "judge_models": "models"}`，告诉调用方哪些键的值需先解析成 id。`apply_node_config` 对这些键期望收到**已解析的 id**（int / list[int]）。

### 4.2 gf CLI 重构（去重，行为不变）

`cli/commands/node.py`、`workflow.py` 改为：先按 `RESOLVE_KEYS` 用 `cli.resolve` 把名解析成 id，再调 `graph_ops.*`（catch `GraphOpError` → `die`）。`build_op` 等迁走后从 graph_ops 导入。**既有 CLI 测试守住行为不变。**

### 4.3 持久化 service `update_workflow_graph(session, wf, graph)`

从 workflows 的 `PUT /api/workflows/{id}` router body 抽出来。**已核实**（`workflows.py:111-120`）现 PUT = 归属校验 + 写 `graph_json` + `commit` + `publish(user.id, "workflow", wf.id)`，**无图校验**（图校验在 run 时由 runner 做，Phase 1 不改这点）。故 `update_workflow_graph(session, wf, graph)` = 写 `graph_json` + `commit` + `publish`。router 改为 delegate；新工具也调它。**工具 / CLI（经 REST）/ 前端三方共用同一条落库 + 通知路径**，画布据 `workflow` 事件调和。

### 4.4 新 toolkit `GraphToolkit(session_factory, user_id)`

仿 `AgentToolkit`：每个写入工具一个 async 方法，体内：
1. `async with session_factory() as s:` 取 `Workflow`，校验 `wf.user_id == user_id`（否则返回「工作流不存在」）。
2. 对需解析的键，按 `RESOLVE_KEYS` 用 `resolve_ref(s, user_id, kind, ref)`（新小助手：纯数字按 id，否则按名精确匹配，复用 `cli.Cli.resolve` 的语义但走 DB）解析。
3. 调 `graph_ops.*` 改 graph dict。
4. 调 `update_workflow_graph(s, wf, graph)` 落库 + 发事件。
5. 返回人话结果串（成功/错误）。

`.tools` property 返回方法列表。在 `system.py:_make_tools` 里 `+ GraphToolkit(self._session_factory, self._user_id).tools`（`_user_id`/`_session_factory` 已在 AgentSystem 持有）。读取工具用现有 `make_node_info_tools`/`make_catalog_tools` 装配进来（按需，只接缺的）。

## 5. 数据流 / 持久化 / 错误处理

- **改图即时反映画布**：工具→`update_workflow_graph` 发 `workflow` 事件→画布现有 SSE 调和（无本地改动自动重载，有改动提示）。**Phase 1 零前端改动。**
- **错误处理**：`graph_ops` 抛 `GraphOpError`；工具 catch 后返回人话错误串（对齐现有工具 `Error: …` 风格，**不抛到框架**，让 agent 能读懂并改正）。归属不符返回「工作流不存在」。
- **「当前工作流」无隐藏态**：工具一律显式收 `workflow_id: int`，不耦合 gf 的 `GF_STATE_FILE`。Agent 先 `list_workflows` 拿 id 再传。
- **观测**：工具调用经现有 `wrap_tools`/`FunctionToolset` 自动渲染成 `AgentToolContent`（tool/args_brief/output_brief），agent 面板已会显示。

## 6. 测试策略

- **graph_ops 纯函数单测**：add/remove/connect/disconnect/apply_node_config（各键）/add_op/remove_op + 错误分支（rescan 非 qc 出发、边查重、未知键、节点不存在、op 序号越界等）。
- **GraphToolkit 工具测**（session_factory 夹具 + user_id）：断言 DB 图被正确改写；**跨租户拒绝**（他人 workflow_id 返回「工作流不存在」、不改数据）；名→id 解析正确。
- **CLI 回归**：`node set/show/prompt/add/rm/link/unlink/op add/ls/rm` 行为与重构前一致（既有 CLI 测试 + 必要补测）。
- **活体（重启后人工）**：主 Agent 用工具从零搭一条小链路（input→llm→output、连线、设模型/提示词），跑通；建即删回基线。

## 7. 不在本期范围（Out of Scope）

- 节点助手任何改动（Phase 3）。
- run / model / dataset / prompt 的写入工具（Phase 2）。
- `restore_workflow_from_run`、`wf export/import` 工具（Phase 2）。
- 移除 `run_command` 跑 gf 的能力（已定：保留作通用 shell）。
- 任何「假运行 / dry_run / 试跑」（项目硬约束，禁止）。

## 8. 风险 / 留观

- **CLI 重构回归**：图变更逻辑搬家可能引入行为漂移——靠既有 CLI 测试 + 纯函数单测双保险；搬家时逐函数对照。
- **工具数量**：Phase 1 新增约 11 写 + 数个读 ≈ 15 个工具进主 Agent，叠加 AgentToolkit/SkillsToolkit 现有工具，系统提示体量上升。可接受（用户已选细粒度）；Phase 2 再增时需复查工具选择可靠性。
- **`update_workflow_graph` 抽取**：已核实现 PUT router 发 `publish(user.id, "workflow", wf.id)`（`workflows.py:120`），抽取后保持即可，画布调和链路成立。
