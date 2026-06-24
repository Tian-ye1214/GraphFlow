# 前端 7 项修复 + HTTP 节点重构 设计

日期：2026-06-24
状态：已与用户对齐（4+3 轮澄清），待审

## 背景与目标

用户提出 7 项问题，多为前端体验缺陷，1 项（HTTP 节点）含后端重构。本批次一次性收口。
贯穿原则：KISS、复用优先、不引入假运行（dry_run）、全程 TDD、不推 origin、提交不带 claude 尾注。

技术栈现状（探查确认）：React + TypeScript + Ant Design + `@xyflow/react` v12.11.0（React Flow）+ `react-markdown`。
页面状态多用本地 `useState`；唯一自定义外部 store 是 `nodeAssistantStore`（`useSyncExternalStore`）。
图以 `graph_json`（verbatim JSON blob）存取：`update_workflow` 直接 `json.dumps(body.graph)` 落库，
GET 直接 `json.loads` 返回，引擎只 `parse_graph` 取 `id/type/config`。

---

## ① 节点改名 → 可编辑「显示名」（纯前端）

**决策**：节点 `id` 一律不动（被边 source/target、DB 索引、磁盘产物目录 `runs/<run>/<id>/`、列血缘字典 key 深度引用）。
新增可选「显示名」`label`，画布与抽屉标题显示 `label || id`，引擎/血缘/产物/CLI 全程忽略它。

**关键事实**：`label` 作为节点 dict 的额外键，随 `graph_json` verbatim 往返 PUT/GET **和** 导出/导入
（`export_package` 用 `json.loads(graph_json)` 原样进 manifest，`import_package` 原样 `json.dumps` 落库；
`_rewrite_refs` 只改 cfg 不碰 label）。`parse_graph` 忽略多余键、`validate_graph` 不看 label。
**故后端零改动、零迁移。**

**改动点（全前端）**：
- `frontend/src/api/types.ts`：`GraphNode` 加 `label?: string`。
- `frontend/src/canvas/serialize.ts`：
  - `toFlow`：节点 `data` 增加 `label: n.label`（→ `data: { config: n.config, label: n.label }`）。
  - `fromFlow`：节点 dict 增加 `label`（从 `n.data.label` 取，空串/undefined 则省略键，避免污染指纹）。
  - 注意：`fromFlow` 当前显式重建 dict 只含 id/type/position/config —— 必须显式加 label，否则丢失。
- `frontend/src/canvas/nodeTypes.tsx`：`GFNode` 第 18 行 `{id}` → `{(data as any)?.label || id}`；
  `NodeProps` 解构加 `data`。
- `frontend/src/pages/CanvasPage.tsx`：
  - 抽屉标题 `${NODE_LABELS[type]}（${selected.id}）` → 显示 `label || id`，并把 id 作为副标题小字。
  - 抽屉内（或 `NodeConfigForm` 顶部）加「显示名」`Input`，改动走 `updateConfig` 同级的新 `updateLabel`：
    `setNodes(ns => ns.map(n => n.id===selectedId ? {...n, data:{...n.data, label}} : n))`，触发既有防抖自动保存。
- 指纹：`graphFingerprint` 基于 `fromFlow` 结果——label 进指纹后改名会触发自动保存 PUT，符合预期。

**测试**：`serialize.test`（label round-trip toFlow→fromFlow）；GFNode 渲染 label||id；改名后 fingerprint 变化。

**非目标**：CLI 改名（`gf node label`）留作后续；改真实 id 不做。

---

## ② 提示词库重排（方向 A）+ ⑥ md 预览滑动窗口

**现状**（`frontend/src/pages/PromptsPage.tsx`）：左 260px 列表 | 右栏把「名称/描述/保存/复制/删按钮行（Space wrap）」
+「编辑 TextArea｜md 预览 左右分栏」+「版本历史」+「被引用」全堆叠，无层次，拥挤。md 预览框已 `overflow:auto`
但与 18 行编辑区同高、无固定高度。

**设计（方向 A）**：右栏自上而下三段——
1. **顶部工具栏**：名称、描述、保存（`sel?'保存（新版本）':'保存'`）、复制为新提示词、删除（Popconfirm 保留 used_by 提示）。
2. **编辑｜预览 左右分栏**（`flex`）：左 = 正文 TextArea + 变量声明行；右 = md 预览，**固定高度 + `overflow:auto` 滑动窗口**
   （解决 ⑥；高度用 `calc` 或定值如 `420px`，与左编辑区视觉对齐）。
3. **底部可折叠元信息区**（Ant `Collapse`，默认折叠或展开变量）：`变量` / `版本历史`（回滚） / `被引用`（used_by）。

**改动点**：`PromptsPage.tsx` 第 56-118 行整段重排（仅 JSX/样式与分区，**不改数据流**：list/sel/save/remove/duplicate/rollback/openDetail 全沿用）。
复用 `Collapse`（项目它处已用）；预览仍 `<ReactMarkdown>{body}</ReactMarkdown>`。

**测试**：`PromptsPage` 既有前端测试若断言结构需同步；新增/调整断言覆盖工具栏按钮、预览容器存在、折叠区渲染。

---

## ③ 对话去左右对齐 → 颜色 + 角色标签（两处都改）

**现状**：
- 节点助手 `NodeConfigForm.tsx` 第 374-384 行：`textAlign: m.role==='user'?'right':'left'` + inline-block span（蓝/绿底）。
- 主 Agent `AgentDrawer.tsx` 第 193-198 行：user 消息 `textAlign:'right'` + 蓝色气泡；assistant 第 201-211 行已全宽 markdown；
  tool 第 178-188 行折叠条。

**设计**：移除按 role 的左右对齐，改**全宽行**；每条消息前置**角色标签**（`你` / `助手`；tool 保持现有 ⚙ 折叠条不变），
保留蓝(`#e6f4ff`,用户)/绿(`#f6ffed`,助手)底色作主要区分。全宽块（非 inline-block 气泡），`whiteSpace:pre-wrap` 保留。

**改动点**：
- `NodeConfigForm.tsx` 第 375 行：去 `textAlign` 三元；span 改块级、加角色小标签。
- `AgentDrawer.tsx` `renderMessage` 第 193-198 行 user 分支：去 `textAlign:'right'`，改全宽块 + `你` 标签 + 蓝底；
  assistant 分支加 `助手` 标签（保留 markdown + 确认删除按钮）。

**测试**：渲染断言无 `textAlign:'right'`；用户/助手消息含角色标签文本。

---

## ④ 节点助手「每节点多会话」+ 面板管理（纯前端，localStorage）

**现状**（`nodeAssistantStore.ts` + `NodeConfigForm.tsx` NodeAssist 第 351-398 行）：
每 key（`graphflow.nodeAssistant.v1:${wf}:${type}:${node}`）单条会话；**messages 不持久化**（仅 draft+modelConfigId 入 localStorage，
F5 即丢 messages）；无会话切换。主 `AgentDrawer` 已有成熟会话模式（会话 Select / 新建 / 删除会话 / 清空全部）可镜像。

**决策（每节点多会话）**：清除上下文 = 新开一条空会话；旧会话仍可在该节点下拉切回；消息持久化到 localStorage。

**store 重构（`nodeAssistantStore.ts`）**：
- 类型：`Conversation = { id: string; title: string; messages: AssistMsg[] }`；
  `NodeAssistState = { conversations: Conversation[]; activeId: string; draft: string; pending: boolean; modelConfigId?: number }`。
- `id`：用 `crypto.randomUUID()`（应用运行期代码，`Date.now()/Math.random()` 仅在 Workflow 脚本里被禁用，此处不受限）。
  `title`：取该会话首条 user 文本前 ~20 字，空则「新会话」。
- **持久化**：`persist` 改为序列化 `{ conversations, activeId, modelConfigId }`（含 messages）；`restore` 还原它们。
  draft 仍持久化。容错：解析失败或结构非法 → 退回单空会话（沿用现有 try/catch 降级风格）。
- 新 API：`newConversation(key)`（= 清除上下文：push 空会话设 active）、`switchConversation(key, id)`、
  `sendAssist` 改为往 `activeId` 会话追加。`get`/`set`/`emit` 机制不变。
- 边界：空会话列表（首次）惰性建一条；删某节点不主动清 localStorage（与现状一致，体积可忽略）。

**UI（NodeAssist 组件 第 351-398 行）**：
- 顶部加一行：会话 `Select`（options=conversations 的 title）+「新会话」按钮（= 清除上下文）。镜像 AgentDrawer 标题栏样式。
- 消息列表渲染 `active.messages`（配合 ③ 的全宽+标签布局）。

**后端**：零改动（节点助手始终是 stateless 单轮请求，history 由前端传）。

**测试**（vitest）：新会话清空当前可见消息但旧会话保留并可切回；messages 持久化 round-trip；
损坏 localStorage 降级单会话；sendAssist 追加到 active 会话。

---

## ⑤ 加节点跟随视口（前端）

**现状**（`CanvasPage.tsx` 第 75-80 行 `addNode`）：`position:{x:80+ns.length*50, y:80+ns.length*40}` 死算对角线，无视视口。

**设计**：新节点落在**当前可见视口中心**，加小幅错位防完全重叠。React Flow v12：`useReactFlow()` 提供
`screenToFlowPosition(screenXY)`；用 ReactFlow 容器 `getBoundingClientRect()` 的中心屏幕坐标换算成 flow 坐标。

**改动点**：
- `CanvasPage.tsx`：`Canvas` 内（已在 `ReactFlowProvider` 下）加 `const rf = useReactFlow()` 与容器 `ref`（挂在包裹 ReactFlow 的 div 或用 `.react-flow` 容器）。
- `addNode`：
  ```
  const rect = wrapperRef.current!.getBoundingClientRect()
  const c = rf.screenToFlowPosition({ x: rect.left + rect.width/2, y: rect.top + rect.height/2 })
  const k = ns.length % 6
  position: { x: c.x - 65 + k*24, y: c.y - 20 + k*24 }   // -半宽/-半高居中 + 错位
  ```
  空画布（`fitView` 默认视口）也落可见区中心，正常。

**测试**：addNode 用 mock `screenToFlowPosition` 验证落点取自视口换算（非硬编码 80+len*50）。
（React Flow 测试可能需 mock；若既有测试不覆盖画布，加轻量单测或在计划中评估可测性。）

---

## ⑦ HTTP 节点重构（接口/params/body+格式/headers高级）+ 节点助手

### 7.1 config 契约变更

| 旧键 | 新键 | 说明 |
|---|---|---|
| `url` | `endpoint` | 接口地址，支持 `{{列}}`。运行时 `endpoint or url` 兼容旧节点 |
| —（新） | `params` | dict，查询参数，值支持 `{{列}}`；**api_key 即其中一个键**。运行时合并进 endpoint 查询串 |
| `body` | `body` | 不变，模板字符串 |
| —（新） | `body_format` | `json`/`raw`/`form`，自动带 Content-Type（json→application/json，form→application/x-www-form-urlencoded，raw→text/plain），multipart 不做 |
| `headers` | `headers` | 保留，UI 收进「高级」折叠区 |
| `method/extract/timeout/retries/concurrency/drop_columns` | 不变 | |

### 7.2 后端改动

- `backend/app/engine/nodes.py` `run_http_fetch_row`（第 224-242 行）：
  - `endpoint = render_template(config.get("endpoint") or config.get("url",""), base)`。
  - `params`：`{k: render_template(str(v), base) for k,v in (config.get("params") or {}).items()}`；
    用 `httpx.URL(endpoint).copy_merge_params(params)` 合并进查询串（保留 endpoint 自带 query；空 params 短路）。
  - `headers`：渲染现有 headers；按 `body_format` 注入 `Content-Type`（**仅当用户未在 headers 显式设置** Content-Type 时）。
  - `body` 渲染不变；调用 `http.fetch(method, str(merged_url), headers, body, ...)`。
- `backend/app/services/http.py`：**签名不变**（url/headers/body 在 nodes.py 已构造好）。
- `backend/app/engine/runner.py` `validate_node_config_shape`（第 394-402 行 http 分支）：
  - 校验 `endpoint`（或 `url`）为 str；`params`（若有）为 dict；`body`（若有）为 str；
    `body_format`（若有）∈ {`json`,`raw`,`form`}；`headers`（若有）为 dict；`extract`（若有）为 dict。点名节点报 ValueError。
- `backend/app/engine/columns.py`：http 输出列 = 输入 ∪ `extract.keys()`，**不变**（params/body 不产列）。
- `backend/app/services/workflow_package.py` `redact_secrets`（第 145-177 行）：
  - 现脱敏 headers/url/body → 扩展：`endpoint`（按 URL 脱敏，复用 `_redact_url`）+ `url`（兼容旧）+
    新增 `params`（dict：敏感键名如 `api_key`/`token`，命中 `_SENSITIVE` 且 `_is_secret_value` → REDACTED，登记 `field=params.<k>`）。
    `api_key` 命中正则中的 `key`。
- 展示类（次要）：`backend/app/agent/node_info.py` `_summarize_node`（method/url/extract → 增 endpoint/params 概览）；
  `backend/app/cli/client.py` `HTTP_STR_KEYS` 加 `endpoint`、`summarize()` http 行改用 endpoint。

### 7.3 前端表单（`NodeConfigForm.tsx` HttpFetchForm 第 809-862 行）

- 「请求」组：方法（GET/POST 保留）；**接口** TextArea（`endpoint`，加载时 `config.endpoint ?? config.url ?? ''` 兼容旧）；
  **Params** `KvEditor`（`config.params`，值占位提示「值，可用 {{列}}；如 api_key」）。
- body（POST 显示）：加 `body_format` `Radio.Group`（JSON/原始文本/表单），TextArea 占位随格式变。
- 「鉴权与提取」组重命名为「提取」；**Headers `KvEditor` 移入「高级」折叠组**（与并发/重试/超时同组），默认折叠。
- 顶部挂 `NodeAssist`：`<NodeAssist nodeType="http_fetch" workflowId nodeId config onApply={c=>onChange({...config,...c})} />`
  （同 LlmSynthForm 第 413 行模式）。
- `MissingColsWarning` 对 endpoint/body 保留；params 值也可加。

### 7.4 节点助手接入 http_fetch

- `backend/app/routers/agent.py` 第 369 行：`("llm_synth","qc")` → `("llm_synth","qc","http_fetch")`。
- `backend/app/agent/codegen.py` 第 54-57 行 `NODE_ASSIST_INSTRUCTIONS`：加 `"http_fetch": load_prompt("node_assist_http_fetch.md")`。
- 新建 `backend/app/agent/prompts/node_assist_http_fetch.md`：镜像 llm_synth/qc 提示词契约
  （输出 `{reply, config}`；config 用新键 endpoint/params/body/body_format/headers/extract）。提示模型：
  - 懂 `{{列}}` 模板与 JSON 路径提取语义（`data.x.0.y`）；
  - api_key 放 params；鉴权头放 headers；
  - **绝不在 reply 里回显 headers/params 的密钥值**（安全）；
  - 不确定接口时可反问澄清（多轮，本轮 config=null）。
- 复用既有 11 只读工具基建（preview/node_info/catalog），无需新工具。

### 7.5 测试

- `backend/tests/test_http_node.py`：改造为新契约 —— endpoint+params 合并查询串、api_key 进 query、
  body_format→Content-Type、旧 `url` 兼容、headers 在高级仍生效、校验各脏 config→ValueError 点名。
- `test_agent_api.py`：node-assist 放行 http_fetch + 返回 {reply,config}；脏输入降级不 500。
- `test_workflow_package`：params/endpoint 含 api_key/token 导出被 REDACTED；模板值放行。
- 前端：HttpFetchForm 渲染 endpoint/params/body_format/headers(高级)/NodeAssist。

---

## 贯穿约束 / 非目标

- **复用优先**：节点助手会话镜像 AgentDrawer 模式；提示词库复用 Collapse；HTTP 助手复用 node-assist 基建与 11 工具；脱敏复用 `_redact_url`/`_SENSITIVE`。
- **KISS / YAGNI**：不做 CLI 改名、不做 multipart、body `form` 仅设 Content-Type（正文仍模板字符串，键值由用户/助手写）、HTTP 方法仍 GET/POST。
- **不引入假运行**；不推 origin；提交不带 claude 尾注；改完线上需重启生效。
- **测试**：后端 pytest 全绿（当前基线 ~715），前端 vitest + `tsc` clean。

## 文件改动清单（汇总）

后端：`engine/nodes.py`、`engine/runner.py`、`services/workflow_package.py`、`routers/agent.py`、
`agent/codegen.py`、`agent/prompts/node_assist_http_fetch.md`(新)、`agent/node_info.py`、`cli/client.py`、
`tests/test_http_node.py`、`tests/test_agent_api.py`、`tests/test_workflow_package*.py`。

前端：`api/types.ts`、`canvas/serialize.ts`、`canvas/nodeTypes.tsx`、`pages/CanvasPage.tsx`、
`pages/PromptsPage.tsx`、`agent/nodeAssistantStore.ts`、`canvas/forms/NodeConfigForm.tsx`、`agent/AgentDrawer.tsx`
（+ 对应 `*.test.tsx`）。

后端 ①/②/③/④/⑤ 零改动（纯前端）；后端仅 ⑦ 触及。
