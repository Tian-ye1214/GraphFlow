# GraphFlow 设计文档

日期：2026-06-10
状态：已确认（20 问需求澄清 + 架构方案 A 经用户批准）

## 1. 项目概述

GraphFlow 是一个面向**大模型训练数据合成**的可视化跑数平台。用户在画布上拖拽节点，编排"LLM 合成 → 代码/规则处理 → 质检回扫"的数据管道，后端高并发执行并落盘。形态类似 Dify，但专注批量数据生产。

- 用户规模：30~50 人（内网部署）
- 单任务数据量：上万行
- 并发模型：每用户一个全局 LLM 并发上限（默认 8，可配置）

## 2. 设计原则

1. **KISS**：最简单可工作的实现优先。不为未发生的 bug 写防御代码；不做投机抽象。具体体现：
   - 进度获取用前端轮询，不做 SSE/WebSocket 推送（批量任务 2 秒粒度足够）；
   - 执行引擎直接写在 FastAPI 进程内，不为"未来分布式"预留 worker 抽象层；
   - UI 仅中文，不引 i18n 框架；
   - 代码节点运行期出错即节点失败、展示 traceback，不做运行期自动修复循环（修复在配置期人机协作完成）。
2. **用户隔离在数据层兜底**：所有业务表带 `user_id`，API 层强制按当前会话用户过滤。
3. **行级落盘，token 不浪费**：每行处理结果即时写库，断点续跑绝不重跑已成功的行。

## 3. 需求结论（20 问摘要）

| # | 主题 | 结论 |
|---|------|------|
| 1 | 场景 | 大模型训练数据合成 |
| 2 | 规模 | 30-50 用户；单任务上万行；用户级 LLM 并发上限（默认 8） |
| 3 | 认证 | 公司 SSO（协议待定）→ 可插拔认证，先做 dev 模式 |
| 4 | 模型 | OpenAI 兼容协议；base_url + api_key 由用户自配 |
| 5 | 参数 | 常用项：temperature / top_p / max_tokens / JSON 模式 / 超时 |
| 6 | 失败 | 指数退避自动重试 → 标记失败行、任务继续 → 一键重跑失败行；token 只统计不限额 |
| 7 | 数据形态 | 行式记录表（JSONL 风格，每行一个 JSON 对象，列自由增减） |
| 8 | 数据源 | JSONL / CSV / Excel / JSON，多文件上传 |
| 9 | 合成节点 | `{{列名}}` 模板；支持一进一出与一进多出；输出整段存列或 JSON 解析拆列 |
| 10 | 代码节点 | 自然语言 → LLM 生成 Python → 用户预览/编辑/确认后保存执行 |
| 11 | 代码执行 | 子进程 + 超时强杀（Linux 加内存上限）；内网可信环境，不做 Docker 沙箱 |
| 12 | 自动处理 | 去重 / 过滤 / 字段重命名删除拼接 / 类型转换 / 采样 / 合并拆分 / 打乱 |
| 13 | 拓扑 | DAG + 回扫边构成受控环（有向有环图） |
| 14 | 生命周期 | 后台队列、关浏览器照常跑、取消、暂停/恢复、断点续跑 |
| 15 | 可观测 | 节点级进度计数 + 结果分页预览 + 失败行列表 |
| 16 | 版本 | 工作流模板、运行历史配置快照、克隆/导入导出 |
| 17 | 质检 | 规则 + LLM-as-judge，均可选、可不选 |
| 18 | 回扫 | 不合格行携带原因回上游重新生成，N 轮上限，超限进最终失败桶 |
| 19 | 快速构建 | mermaid 文本 ↔ 画布双向 + 自然语言生成工作流 |
| 20 | 平台 | Windows 开发 / Linux 部署；React + React Flow；SQLite WAL |

## 4. 总体架构

**方案 A：单进程一体化**（已批准）。FastAPI 进程内嵌 asyncio 执行引擎 + SQLite(WAL) + 子进程池（执行 LLM 生成的代码）。

理由：LLM 调用是纯 IO 密集，50 用户 × 8 并发 = 400 个挂起 HTTP 请求，asyncio 单进程足够；CPU 密集操作进子进程池不阻塞事件循环；行级断点续跑天然兜底进程重启。部署只需一个 uv 进程 + 一个静态目录。

```
GraphFlow/
├── frontend/          React 18 + TypeScript + Vite
│                      React Flow（画布）+ Zustand（状态）+ Ant Design 5（组件）
├── backend/           FastAPI + uv（Python 3.12）
│                      SQLAlchemy 2(async) + aiosqlite + Alembic
│                      openai SDK(async，自定义 base_url) + pandas + openpyxl
└── docs/
```

- 开发（Windows）：`vite dev`（代理 /api）+ `uv run fastapi dev`
- 生产（Linux）：`vite build` 产物由 FastAPI StaticFiles 托管，单进程交付
- 路径全程 pathlib，数据目录由环境变量配置，无平台耦合

### 认证（可插拔）

`AuthProvider` 接口 + 当前唯一实现 `DevAuthProvider`（输入用户名即登录，自动建用户）。会话用签名 Cookie。公司 SSO 协议确认后再增加对应实现——在此之前不编写 OIDC/LDAP 代码（KISS）。

## 5. 数据模型（SQLite，WAL 模式）

| 表 | 字段要点 |
|----|---------|
| `users` | id, username, display_name, auth_provider, max_llm_concurrency(默认 8), created_at |
| `model_configs` | id, **user_id**, name, base_url, api_key_enc（Fernet 加密）, default_params_json |
| `datasets` | id, **user_id**, name, source('upload'/'run'), original_filename, file_path, row_count, columns_json, created_at |
| `dataset_rows` | id, dataset_id, idx, data_json |
| `workflows` | id, **user_id**, name, graph_json, is_template, created_at, updated_at |
| `workflow_versions` | id, workflow_id, version, graph_json, created_at（运行时快照） |
| `runs` | id, **user_id**, workflow_id, workflow_version_id, status, stats_json（token 用量等）, error, created_at, started_at, finished_at |
| `run_node_states` | id, run_id, node_id, status, total, done, failed, updated_at（节点进度条数据源） |
| `run_rows` | id, run_id, node_id, row_idx, attempt, qc_round, status, data_json, error, updated_at（**行级断点核心表**） |

- run.status：`queued / running / paused / cancelled / completed / failed`
- run_rows.status：`pending / running / done / failed / qc_failed_final`
- 上传的原始文件与导出文件存磁盘（数据目录下按 user_id 分目录），元数据入库。

### 工作流图 JSON 结构

```json
{
  "nodes": [{ "id": "n1", "type": "llm_synth", "position": {"x": 0, "y": 0}, "config": { ... } }],
  "edges": [{ "id": "e1", "source": "n1", "sourceHandle": "pass", "target": "n2", "kind": "normal" }]
}
```

`edge.kind`：`normal` 或 `rescan`（回扫边）。图校验规则：**仅由 normal 边构成的子图必须无环；环只能经由 rescan 边形成**。

## 6. 节点体系（6 种）

| 节点 | type | 关键配置 |
|------|------|---------|
| 输入 | `input` | 选择 1~N 个数据集，多选时按行拼接 |
| LLM 合成 | `llm_synth` | system_prompt、user_prompt（支持 `{{列名}}` 模板）、模型选择、参数（temperature/top_p/max_tokens/JSON 模式/超时）、节点并发数（受用户上限钳制）、扇出数 N（一条种子生成 N 条变体）、输出映射（整段→指定列 / JSON 解析→多列）、重试次数 |
| LLM 写代码 | `llm_code` | 自然语言指令；配置期：采样数据样例 → LLM 生成 Python 函数 `def process(df: pd.DataFrame) -> pd.DataFrame` → 预览/编辑 → 可在样例上试跑 → 报错可让 LLM 基于 traceback 修复 → 确认保存代码进 config；运行期执行已保存代码 |
| 自动处理 | `auto_process` | 操作清单（可叠加）：去重（按列）、过滤（长度/关键词/正则）、字段重命名/删除/拼接、类型转换、随机采样、打乱；输入节点已承担合并，拆分由过滤实现 |
| 质检 | `qc` | 规则检查（长度/格式/正则/必填列，可多条）+ LLM-as-judge（裁判模型、评分维度与阈值），两类均可选/可不选；两个出口：`pass` / `fail`；fail 出口仅允许接 rescan 边；max_rescan_rounds（默认 3） |
| 输出 | `output` | 导出格式（JSONL/CSV/Excel）或保存为新数据集 |

## 7. 执行引擎

### 调度语义（行/批混合）

- **行级节点**（llm_synth、qc、auto_process 中的纯行操作）：逐行流式处理，行完成即流向下游，形成流水线并行；
- **批级节点**（去重、采样、打乱、llm_code、output）：屏障语义，等上游全部行到齐后整表处理一次。
- 节点声明自己的模式（auto_process 依所选操作而定：含去重/采样/打乱任一批级操作时整节点按批级执行，否则行级），引擎按拓扑序混合调度。

### 并发控制

每用户一个全局 `asyncio.Semaphore`（容量 = users.max_llm_concurrency），该用户所有运行中的 LLM 调用共享；节点配置的并发数在此之上再做局部钳制。

### 回扫环

1. 质检 fail 出口经 rescan 边连回上游某生成节点；
2. 不合格行写入 `_qc_reason`、`_qc_round` 两列后回流，生成节点 prompt 可引用 `{{_qc_reason}}`（带着失败原因重新生成）；
3. 每行回扫次数 ≤ max_rescan_rounds，超限标记 `qc_failed_final` 进最终失败桶（结果中可见、可导出）；
4. 回扫只重跑该行经过的回扫路径段，不影响其他行。

### 生命周期

- 点"运行"→ 任务入后台队列（asyncio.Task），关浏览器照常执行；
- **取消**：协作式——引擎在行间检查取消标志，停止后保留已完成结果；
- **暂停/恢复**：同机制，暂停置位后行间挂起，恢复即继续；
- **断点续跑**：`run_rows` 逐行落盘；进程重启后扫描 `running` 状态的 run 自动恢复，已 `done` 的行直接跳过；
- **失败处理**：LLM 调用指数退避重试（节点配置次数）→ 仍失败则该行 `failed`、任务继续 → 运行结束后可"一键重跑失败行"：在原 run 内把 `failed` 行重置为 `pending` 后重新入队，复用断点续跑机制，不新建 run。

### 进度与结果

前端每 2 秒轮询 `GET /api/runs/{id}`（含各节点 total/done/failed 计数）；运行详情页提供按节点的结果分页预览、失败行列表（含错误原因）。token 用量累计进 `runs.stats_json`，只统计不限额。

## 8. Mermaid DSL 与自然语言生成

```
flowchart LR
  in[输入: seed.jsonl]
  gen[LLM合成: 扩写问答对]
  clean[自动处理: 去重+过滤]
  qc{质检: 规则+评分}
  out[导出: jsonl]
  in --> gen --> clean --> qc
  qc -->|pass| out
  qc -.->|fail| gen
```

- **文本 → 画布**：解析 mermaid flowchart 子集：节点形状/中文前缀标识节点类型（`[输入:...]`、`[LLM合成:...]`、`{质检:...}` 等），`-->` 为 normal 边，`-.->` 为 rescan 边。生成拓扑骨架与节点标题，详细参数在侧边栏补全。
- **自然语言 → 画布**：用户描述需求 → 用用户自己配置的模型生成完整工作流 JSON → 校验后载入画布。
- **画布 → 文本**：一键导出 mermaid（拓扑视图，可贴文档）与完整 JSON（含全部参数，可导入复现）。

## 9. API 概览

| 分组 | 端点 |
|------|------|
| 认证 | `POST /api/auth/login`（dev 模式），`GET /api/me` |
| 模型配置 | `GET/POST/PUT/DELETE /api/models`，`POST /api/models/{id}/test`（连通性测试） |
| 数据集 | `POST /api/datasets/upload`（multipart 多文件），`GET /api/datasets`，`GET /api/datasets/{id}/rows?page=`，`DELETE /api/datasets/{id}` |
| 工作流 | CRUD `/api/workflows`，`GET /api/workflows/{id}/versions`，`POST /api/workflows/import`，`GET /api/workflows/{id}/export`，`POST /api/workflows/from-mermaid`，`POST /api/workflows/from-nl`，`GET /api/workflows/{id}/mermaid` |
| 代码生成 | `POST /api/codegen/generate`，`POST /api/codegen/fix`（配置期使用） |
| 运行 | `POST /api/runs`，`GET /api/runs`，`GET /api/runs/{id}`（含节点进度），`POST /api/runs/{id}/cancel|pause|resume|rerun-failed`，`GET /api/runs/{id}/rows?node_id=&status=&page=`，`GET /api/runs/{id}/export` |

所有业务端点强制按会话用户过滤数据。

## 10. 安全

- **api_key**：Fernet 对称加密落盘，密钥来自环境变量；API 响应与日志永不回显明文。
- **代码执行**：子进程运行 + 超时强杀；Linux 上加 `resource.setrlimit` 内存上限（一行配置）；Windows 开发环境仅超时。内网可信环境，不做更重的沙箱。

## 11. 测试策略

- 后端 pytest + pytest-asyncio：执行引擎单测（拓扑调度、回扫轮次上限、断点恢复、用户级信号量）、API 集成测试（httpx ASGI transport，含用户隔离断言）；
- 前端 vitest + React Testing Library：节点配置表单、mermaid DSL 解析；
- 画布交互以手工验证为主，不强求 e2e 自动化（KISS）。

## 12. 里程碑

| 阶段 | 内容 |
|------|------|
| P1 核心闭环 | dev 认证、模型配置、数据集上传/预览、画布（输入/LLM合成/自动处理/输出）、DAG 执行引擎、运行队列+进度轮询+取消、失败行重跑、导出 |
| P2 完整节点与回扫 | LLM 写代码节点（生成/试跑/修复）、质检节点 + 回扫环、暂停/恢复/断点续跑、运行历史与版本快照、模板 |
| P3 快速构建与接入 | mermaid DSL 双向、自然语言生成工作流、工作流导入导出、SSO 接入（协议确认后） |

## 13. 悬而未决

- 公司 SSO 协议（OIDC/SAML/LDAP）未定——已用可插拔 AuthProvider 隔离，确认前仅实现 dev 模式，不影响其余开发。
