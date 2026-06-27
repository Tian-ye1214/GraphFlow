# 设计：把全生命周期操作封装成结构化工具（Phase 2 — 主 Agent 的 run/model/dataset/prompt + restore + 导入导出）

日期：2026-06-27
状态：已与用户在 brainstorming 中逐项确认，待 spec 复审

## 0. 背景与缘起

Phase 1（master@f987104，复审补修 master@26685d0）已把 **workflow/node/edge** 操作做成主 RedLotus Agent 的细粒度 pydantic-ai 工具（三层：`graph_ops` 纯函数 / `workflow_store` 落库+SSE / `GraphToolkit` 直连 DB+归属校验）。Phase 2 照同范式**补齐全生命周期**：主 Agent 的 **run / model / dataset / prompt** 写工具 + `restore_workflow_from_run` + **workflow export/import(.gfpkg)** 工具。完成后主 Agent 能：建链路（P1）→ 跑 → 看结果 → 迭代 → restore 版本 → 打包/导入 → 管模型/数据集/提示词库，全程结构化工具，不再靠 `run_command` 拼 `gf` 命令串。

`run_command` 保留作通用 shell（跑脚本等非 gf 命令）；gf CLI 依旧给人用。

## 1. 已锁定的决策（brainstorming 结论）

| 维度 | 结论 |
|---|---|
| 范围 | **一次做全**：run/model/dataset/prompt 写工具 + restore + wf 导入导出（26 个新工具） |
| 复用策略 | **方案 A**：把 REST 内联的写逻辑抽成 service 单点，REST + Agent 共用（符合「复用优先/单点化」铁律）。否决 httpx 调自身 REST（退回 P1 抛弃的 HTTP 跳）/ 直连 DB 重写逻辑（两套真相源违 DRY） |
| 破坏性工具确认门禁 | **5 删除 + restore** 走 confirm_delete 门禁：`delete_dataset`/`delete_model`/`delete_prompt`/`delete_run`/`delete_all_runs` + `restore_workflow_from_run`（覆盖当前图=丢当前编辑）。`start_run`/`rerun_failed`/`cancel_run` 不门禁 |
| api_key 写入 | 原生 `create_model`/`update_model` **可写 key**，但在工具参数渲染/agent 消息日志里**打码**（单点在 `_brief`）。能力对等 `gf model add` 且改善泄漏面 |
| 工具颗粒度 | **细粒度**：一个 gf 子命令 ≈ 一个工具（沿用 P1 口径） |
| toolkit 组织 | **4 个独立 toolkit**（RunToolkit/ModelToolkit/DatasetToolkit/PromptToolkit），各自边界清晰、可隔离测；导入导出 2 工具并进 GraphToolkit |
| 只读复用 | catalog 的 `list_user_datasets`/`list_user_models`/`list_prompts`/`get_prompt` 已在主 Agent，**不重复加**；新 toolkit 只补缺的读（run 读、prompt 版本） |
| 上传/导出沙箱 | dataset 上传/导出、wf 导入导出一律走**会话工作目录**（Agent 先 `write_file` 落文件再 `upload_dataset`；导出/打包写进工作目录） |
| start_run 语义 | 入队即返回 run_id，**不阻塞等待**；Agent 自行 `get_run` 轮询（无假运行/不自动 watch） |
| 推进方式 | Phase 2 一期落地；**节点助手集成仍归 Phase 3** |

## 2. 现状关键事实（决定复用哪一层）

### 2.1 已可直接复用的 service 单点（无需新抽）

| 层 | 单点 | 文件:行 | 用途 |
|---|---|---|---|
| Run | `enqueue_run(session_factory, user_id, workflow_id) -> run_id` | run_service.py:84 | **run 启动入口单点**：快照版本→建 Run→入队 manager |
| Run | `validate_graph_resource_ownership(session, graph, user_id)` | run_service.py:59 | 资源归属校验（不符抛 ValueError） |
| Run | `purge_run_rows(session, run_ids, version_ids=None)` | run_service.py:22 | 级联删 run 子表族 + Run + 版本快照 |
| Run | `unlink_run_exports(run_ids, data_dir)` | run_service.py:35 | 删 run 磁盘导出/artifact |
| Run | `first_round_rate` / `sample_failures` | run_service.py:106 / 116 | QC 聚合/抽样 |
| Run | `RUN_CHILD_MODELS` | run_service.py:16 | run 子表族（新增子表只改这处） |
| Run | `manager.cancel(run_id)` / `manager.submit_prepared(...)` | engine/manager.py | 取消 / 重跑入队 |
| Dataset | `ingest_manager.submit(dataset_id, source_path, unit, version, user_id, session_factory)` | ingest_manager.py:52 | 异步摄入单点 |
| Model | `crypto.encrypt(plain) -> str` | crypto.py:14 | 密钥加密单点 |
| Prompt | `_latest_version(session, prompt_id)` | catalog.py:65 | 查最新版（已被 catalog 复用） |
| Package | `export_package(session, workflow, dest_path)` / `import_package(session, zip_path, user_id) -> (wf_out, report)` | workflow_package.py:189 / 479 | 打包/解包单点；不可信包错误抛 `PackageError(ValueError)`→422 |
| Workflow | `update_workflow_graph` / `delete_workflow_full` | workflow_store.py | P1 已抽，restore 落库可复用 `update_workflow_graph` 语义 |

### 2.2 需新抽的 service 单点（REST 现内联，Phase 2 抽出供 REST+Agent 共用）

- **`restore_workflow_from_run(session, run, user_id) -> Workflow`**：现内联在 `runs.py:241-252`（取 run.workflow_version_id 的版本 graph_json → 覆盖 Workflow.graph_json → commit → publish）。抽进 `workflow_store.py` 或 `run_service.py`，REST 与 Agent 共用。
- **`model_service`**：`create_model` / `update_model`（api_key 加密收口，留空=不改）/ `delete_model`。现内联 `model_configs.py:75/91/106`（加密在 :82）。
- **`dataset_service`**：`delete_dataset`（级联删 DatasetRow + 磁盘分片 + 源文件回收），现内联 `datasets.py:428-454`。upload 复用 `ingest_manager.submit`（REST upload 端点 `datasets.py:197` 的落盘+占位行+提交逻辑按需抽出共用）。
- **`prompt_service`**：`create_prompt` / `update_prompt`（**仅正文变更才追加 PromptVersion**，名/描述原地改）/ `delete_prompt`（级联删版本）/ `rollback_prompt`（复制历史版正文成新版）/ `duplicate_prompt`。现内联 `prompts.py:83/102/116/145/161`。

> 抽取原则同 P1：REST 路由改为 delegate；既有 REST/CLI 测试守住行为不变；逐函数对照防漂移。

### 2.3 工具装配点 & 确认门禁 & 渲染

- `AgentSystem._make_tools`（system.py:44-55）现 = AgentToolkit + Skills + GraphToolkit + catalog（仅 session_factory+user_id 都在时挂后两者，且 GraphToolkit 已透传 `confirm_delete`）。Phase 2 在此追加 4 个新 toolkit，同样透传 `confirm_delete`。
- 确认门禁范式（P1 复审补修已立）：toolkit 构造收 `confirm_delete: bool=False`；破坏性工具在 `not confirm_delete` 时返回需确认串（引导转出 `[confirm_delete] <gf 等价命令>`）不执行；用户回「确认」开头消息→`turns.py:157` confirm_delete=True→当回合执行。
- 工具调用渲染单点 `_brief(kwargs)`（tools.py:40）：现 `k=str(v)[:40]`。**新增密钥打码**：kwarg 名 ∈ {api_key, key, token, secret, password} → 渲成 `***`。

## 3. 工具清单（细粒度 pydantic-ai 工具）

🔒 = 走 confirm_delete 门禁。括注「复用」= 直接调既有 service 单点。

### RunToolkit(session_factory, user_id, confirm_delete=False) —— 11 个

读（直连 DB，归属校验，结果走 `_fit_budget` 防爆 20k）：
| 工具 | 行为 |
|---|---|
| `list_runs(workflow_id=None)` | 列本租户运行（id/工作流名/状态/创建时间/QC 指标），可按 workflow 筛 |
| `get_run(run_id)` | 单次运行状态/统计/节点进度/错误 |
| `read_run_rows(run_id, node_id, status=None, limit=20)` | 分页读某节点输出/失败行 |
| `read_run_logs(run_id, kind="system", node_id=None)` | system 日志 或 model 调用日志 |
| `read_run_qc(run_id, node_id=None)` | QC 指标 + 失败样本（per-model 理由） |

写：
| 工具 | 行为 |
|---|---|
| `start_run(workflow_id)` | 复用 `validate_graph_resource_ownership` + `enqueue_run`；返回 run_id，不阻塞 |
| `cancel_run(run_id)` | 复用 `manager.cancel` |
| `rerun_failed(run_id, node_id=None)` | 复用 manager 重跑入队 |
| `restore_workflow_from_run(run_id)` 🔒 | 复用新抽 `restore_workflow_from_run` |
| `delete_run(run_id)` 🔒 | 复用 `purge_run_rows` + `unlink_run_exports` |
| `delete_all_runs()` 🔒 | 批量清空本租户运行（运行中除外，同 REST 语义） |

### ModelToolkit(session_factory, user_id, confirm_delete=False) —— 4 个
（list 复用 catalog `list_user_models`，不重加）
| 工具 | 行为 |
|---|---|
| `create_model(name, base_url, model_name, api_key=None, provider="openai", api_version=None)` | 复用 `model_service.create_model`；api_key 经 crypto.encrypt；**api_key 为独立顶层参数**便于 `_brief` 打码 |
| `update_model(model_id, name=None, base_url=None, model_name=None, api_key=None, provider=None, ...)` | 复用 `model_service.update_model`；api_key 留空=不改 |
| `delete_model(model_id)` 🔒 | 复用 `model_service.delete_model` |
| `test_model(model_id)` | 复用 REST test 逻辑：真实发一条请求（**产生费用**，文案提示） |

### DatasetToolkit(session_factory, user_id, confirm_delete=False) —— 3 个
（list/preview 复用既有 catalog + preview_workflow_data）
| 工具 | 行为 |
|---|---|
| `upload_dataset(file_path, name=None)` | 从**会话工作目录**读 jsonl/json/csv/xlsx/xls → 复用 upload 落盘 + `ingest_manager.submit` 异步摄入；返回 dataset_id + status=importing |
| `export_dataset(dataset_id, format="jsonl")` | 复用 export 逻辑流式写到**会话工作目录**；返回工作目录内相对路径 |
| `delete_dataset(dataset_id)` 🔒 | 复用 `dataset_service.delete_dataset`（级联库+磁盘+源文件） |

### PromptToolkit(session_factory, user_id, confirm_delete=False) —— 6 个
（list/get 复用 catalog `list_prompts`/`get_prompt`）
| 工具 | 行为 |
|---|---|
| `create_prompt(name, body, description="")` | 复用 `prompt_service.create_prompt`（自动建 v1 + 提取 {{变量}}） |
| `update_prompt(prompt_id, body=None, name=None, description=None)` | 复用 `prompt_service.update_prompt`（仅正文变更才出新版） |
| `delete_prompt(prompt_id)` 🔒 | 复用 `prompt_service.delete_prompt`（级联删版本） |
| `list_prompt_versions(prompt_id)` | 列所有版本号/正文摘要 |
| `rollback_prompt(prompt_id, version)` | 复用 `prompt_service.rollback_prompt`（复制历史版成新版） |
| `duplicate_prompt(prompt_id, name=None)` | 复用 `prompt_service.duplicate_prompt` |

### GraphToolkit 追加 —— 2 个（导入导出）
| 工具 | 行为 |
|---|---|
| `export_workflow(workflow_id)` | 复用 `export_package` 写 .gfpkg 到**会话工作目录**；返回相对路径 |
| `import_workflow(file_path)` | 从工作目录读 .gfpkg → 复用 `import_package`；不可信包错误（PackageError/GraphError）→人话错误串；返回新工作流 id + 复用/新建/缺密钥报告 |

合计 11+4+3+6+2 = **26 新工具**。

## 4. 架构 / 数据流 / 持久化 / 错误处理

- **写工具统一流**：`async with sf() as s:` → 归属校验（他人资源→「不存在」串）→ 调 service 单点 → 落库 commit → `publish` 发对应 SSE 事件（workflow/dataset/model/prompt/run/agent）→ 返回人话结果串。
- **改图/资源即时反映前端**：复用既有 SSE 调和链路（P1 已验）。
- **错误不抛框架**：service 抛 ValueError/GraphOpError/PackageError，工具 catch 后返回 `Error: …` 串（对齐 P1 `_mutate` 与 columns 端点降级）。
- **门禁**：6 个 🔒 工具未确认时返回需确认串（含 `[confirm_delete] <gf 等价命令>`，前端 AgentDrawer 渲染确认按钮→「确认：<cmd>」→当回合执行）。
- **密钥**：`_brief` 按名打码；service 加密落库；catalog 永不返回明文（既有铁律不变）。
- **沙箱**：上传/导出/打包路径一律解析进会话工作目录（复用 AgentToolkit 的工作目录约束，防路径穿越）。
- **start_run 不阻塞**：返回 run_id；Agent 用 `get_run` 轮询（KISS，无假运行、无自动 watch 阻塞回合）。

## 5. 测试策略

- **新抽 service 单测**：model 加密/留空不改、prompt 正文变更才出版本/rollback 复制语义、dataset 级联删（库+磁盘+源文件）、restore 覆盖图+publish。
- **各 toolkit 工具测**（session_factory 夹具 + user_id）：DB 被正确改写；**跨租户拒绝**（他人 id→「不存在」、不改数据）；**门禁未确认不执行**（返回需确认串、资源完好），确认后执行；名→id 解析（如 start_run 资源校验）。
- **密钥打码测**：create_model/update_model 调用后 `_brief` 输出不含 api_key 明文（含 `***`）。
- **REST 回归**：model/dataset/prompt CRUD + restore + 导入导出 delegate 后行为与抽取前一致（既有测试 + 必要补测）。
- **活体（重启后人工）**：主 Agent 从零搭链(P1 工具)→`start_run`→`get_run` 轮询→`read_run_rows` 看结果→建/改/删 prompt→建模型(打码核验)→`restore_workflow_from_run`→`export_workflow`/`import_workflow`→`delete_*` 经确认；建即删回基线；**重点压工具数量(~50+)下模型选工具可靠性**。

## 6. 不在本期范围（Out of Scope）

- 节点助手任何集成（草稿调和 + 作用域写工具）→ **Phase 3**。
- 移除 `run_command` 跑 gf 的能力（保留作通用 shell）。
- 任何假运行 / dry_run / 试跑（项目硬约束）。
- dataset 版本 CRUD 的细粒度工具（create_dataset_version 等）暂不做（niche）；如需经 gf。
- 自动 watch/阻塞等待 run 完成（KISS，靠 get_run 轮询）。

## 7. 风险 / 留观

- **工具数量膨胀**：主 Agent ~50+ 工具（26 新 + GraphToolkit 15 + catalog 4 + 基础 7 + skills），P1 spec 已警告「工具多→模型选工具可靠性下降」。缓解：命名一致（动词_资源）、docstring 精炼、活体重点压选工具可靠性；若实测下降明显，预案=按角色/场景拆装（留观，本期不做）。
- **service 抽取回归**：model/dataset/prompt CRUD 搬家可能行为漂移——既有 REST 测试 + service 单测双保险，逐函数对照。
- **test_model / start_run 真实花钱**：工具 docstring 明示；不加额度护栏（KISS，超出本期）。
- **api_key 深层持久化**：`_brief` 打码覆盖面板/事件；但 pydantic-ai 消息历史（ToolCallPart args）与用户聊天原文仍含 key（同 gf 现状），属固有面，本期只做渲染打码（用户已选）。
