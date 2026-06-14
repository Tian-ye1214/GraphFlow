# 批次五：六项修复与改进 设计

**日期：** 2026-06-14
**分支：** （将新建）`feature/six-fixes`

把用户提出的 6 项需求收敛为一份设计。贯穿约束（KISS 硬规则）：最简实现、不预防未发生的 bug；api_key 全程 Fernet 加密、绝不进响应/日志/提示词；所有模型/工作流/运行引用校验 `user_id`（租户隔离）。

---

## ① Agent 会话删除（纯前端）

**现状：** 后端 `DELETE /api/agent/sessions/{sid}`（`routers/agent.py:160-169`）已就绪——校验归属、`turn_manager.cancel(sid)` 取消在跑任务、级联删 `AgentMessage`+`AgentSession`、`rmtree` 工作目录。前端 AgentDrawer 无删除入口。

**改动：** AgentDrawer 标题栏会话下拉旁加一个删除按钮（Popconfirm）。删成功后从 `sessions` 列表移除；若删的是当前会话，切到下一个或清空 `detail`。

## ② 目标模式 → 配置区开关（纯前端）

**现状：** 目标启动条在底部「发送」上方（`AgentDrawer.tsx:216-225`）。

**改动：** 从底部移除。标题栏（配置区）加一个 `Switch`「目标模式」；打开后在标题下方展开一行（与 advanced 同级）：目标工作流 `Select` + 一句话目标 `Input` + 启动 `Button`。运行中的轮次提示与每轮指标行仍留底部。

## ③ 记录一键全部删除（runs + 会话，per-user）

**现状：** Runs 列表与 Agent 会话都只有单条删除；无批量清空接口。

**改动（后端）：** 新增两个按 `user_id` 作用域的批量删除端点，各自**复用单删的级联顺序**：
- `DELETE /api/runs`：清空当前用户**非运行中**（status 不在 queued/running）的全部 run。级联 `RunRow/RunNodeState/RunLog/QcMetric/QcFailure/Run/WorkflowVersion` + 删导出文件。返回 `{"deleted": n}`。
- `DELETE /api/agent/sessions`：清空当前用户全部会话。先 `turn_manager.cancel` 每个，再级联 `AgentMessage/AgentSession` + `rmtree` 目录。返回 `{"deleted": n}`。

**改动（前端）：** RunsPage 加「清空全部」、AgentDrawer 加「删除全部会话」，均带 Popconfirm，删后刷新。

## ④ codegen 严格禁止真实跑数，只拿列名

**现状（bug）：** `POST /api/agent/codegen` → `gather_sample_rows` 取上游最多 5 行**真实数据** → `generate_with_repair` 在子进程**真跑** AI 代码最多 4 次产出 `preview_rows` → 真实行值还进了 LLM 提示词；前端 `<pre>` 展示转换后的真实数据。`node-assist`（配置助手）同样把真实行值塞进提示词（不执行）。

**改动（后端）：** `codegen.py`
- 新增 `gather_upstream_columns(s, workflow_id, node_id, user_id) -> (columns: list[str], source)`：优先取最近一次运行的上游 `RunRow` 输出行的**键**（真实列名、零值泄漏），否则取上游 input 数据集的 `columns_json`，否则空。复用现有 `_upstream_run_rows` 的图遍历。
- `_user_prompt` 改为只发列名（不发样本值）。
- `generate_with_repair` → 收敛为 `generate_code(model, instruction, columns)`：仅按指令+列名生成代码，**不执行、无 preview、无修复循环**。
- `generate_node_config` 改收 `columns`。
- 删除对 `run_process_code` 的调用（codegen 路径不再 import 它）。
- `INSTRUCTIONS` 提示词从「样本行」改为「上游可用列」。

**改动（端点）：** `codegen` 返回 `{"code", "columns", "sample_source"}`（去掉 `preview_rows`/`error`）；`node-assist` 仍返回 `{"config", "sample_source"}`，但内部走列名。

**改动（前端）：** `AgentOpFields.generate()` 与 `NodeAssist.run()` 改为显示「检测到的上游列：col1、col2…」，移除数据 `<pre>` 与 `preview` 状态。`CodegenOut` 类型改为 `{code, columns, sample_source}`。

**说明：** 真正运行工作流时 AI 代码照常对全量数据执行（引擎 `apply_operations_with_agent` **不动**）。仅去掉写代码阶段的「提前真跑预览」。

## ⑤ 质检空内容误通过修复（兜底 + 提示词 + 温度0，全做）

**根因（实证 run #18 / workflow #2）：** 自动处理删光列 → 空 `{}`；首轮质检全挂（`first_round_pass=0`，符合预期）；但 `qc→(rescan)llm_synth` 回扫把空行重生为空译文，判定模型**未固定温度**，对「结构合法但内容全空」的行随机 3/10 判通过。代码无「默认通过」分支（边界一律失败兜底），属提示词/随机性问题。

**改动（`engine/nodes.py` `run_qc_judge_row`）：**
- **兜底**：构造 `base` 后，若 `not any(str(v).strip() for v in base.values())`（空或全空白）→ 直接返回不通过，**不调用任何 judge**，理由「样本内容为空」。
- **提示词锚定**：给每次 judge 的 `system` 追加固定一句「若待判定内容为空或缺少必要字段，必须返回 pass:false」。不改用户工作流。
- **温度0**：`params` 增加 `"temperature": 0`（`llm.chat` 用 `params` 覆盖模型默认，故判定固定为确定性）。

## ⑥ 节点运行进度条：实时 SSE + 视觉优化

**现状：** `RunNodeState(status,total,done,failed)` 已存在；runner 逐行调 `_set_node_state` 更新；RunDetailPage 每节点已渲染 antd `Progress`，靠 2 秒轮询刷新。tqdm 是终端库，渲染不到网页——不用。

**改动（后端 `engine/runner.py`）：** `_set_node_state` 增加 `user_id` 形参；写库后 `publish(user_id, "run", run_id, kind="progress", data={node_id,status,total,done,failed})`。所有调用点（barrier/llm/qc）都已有 user_id，逐行更新即逐行推送。

**改动（前端 RunDetailPage）：** `useEvents` 订阅：`entity==="run" && id===runId` 时，`kind==="progress"` 就地更新对应节点 state（秒级平滑），无 kind（运行完成）触发整页 refresh。视觉：顶部加一条总进度（Σdone/Σtotal）、节点卡片更清晰、保留 antd Progress 动效。2 秒轮询作为兜底保留。

---

## 测试策略

- 后端（pytest）：批量删除（删全部/跳运行中/租户隔离）、codegen 列名采集（返回列名非值、端点无 preview_rows、不执行）、质检兜底（空行不调 judge 即失败）+ 温度0 透传 + 锚定句存在、进度 publish（状态变更即推 progress 载荷）。
- 前端（`npm run build` + `npx vitest run`）：无组件测试设施，构建+既有单测保绿。

## 文件清单

- 改后端：`routers/runs.py`（批量删）、`routers/agent.py`（批量删会话 + codegen/node-assist 端点）、`agent/codegen.py`（列名采集，去执行）、`engine/nodes.py`（质检兜底+温度0+锚定）、`engine/runner.py`（进度 publish）。
- 改前端：`pages/RunsPage.tsx`（清空全部）、`agent/AgentDrawer.tsx`（会话删除+删全部+目标模式开关）、`canvas/forms/NodeConfigForm.tsx`（展示列名）、`pages/RunDetailPage.tsx`（SSE 进度+视觉）、`api/types.ts`（CodegenOut）。
