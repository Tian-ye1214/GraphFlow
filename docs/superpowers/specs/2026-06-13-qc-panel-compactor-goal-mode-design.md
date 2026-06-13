# 多模型质检 + 上下文 Compactor + 目标优化模式 设计文档

> 日期：2026-06-13 · 状态：已批准设计，待写实现计划

## 总览

三个相互关联的特性，合成一个「可度量、自我改进的质检优化」能力：

- **F1 多模型质检面板（K-of-N）** —— QC 节点从单模型升级为 N 模型同提示词判定 + 「至少 K 个通过才输出」策略。
- **F2 上下文 Compactor** —— 独立 LLM 压缩模块，Agent 上下文达 75% 触发，所有角色统一复用。
- **F3 目标优化模式** —— 简单 while-True 循环，事件驱动跑数，凝练经验式自我改进，结构化阈值客观达标判定 + UI 展示。

**依赖与顺序**：F3 依赖 F1（指标来源 + 失败样本）与 F2（压缩）。实现顺序 **F1 → F2 → F3**。三者放一份设计、一份按序执行的计划。

**贯穿约束（KISS 硬规则）**：最简实现，不预先防御未发生的 bug；不投机抽象。api_key 全程 Fernet 加密，绝不出现在任何响应/日志/Agent 提示词。每用户租户隔离是硬验收项——所有模型引用（判定/回扫/恢复/压缩）均校验 `user_id`。

---

## F1 · 多模型质检面板（K-of-N）

### 配置形态（QC 节点 `config`）

- `judge_model_ids: list[int]` —— N 个判定模型，共用一套 `system_prompt` / `user_prompt`。
- `pass_k: int` —— ≥K 个模型判通过即整行通过；默认 `1`。
- `system_prompt` / `user_prompt` / `concurrency` / `max_rounds` / `retries` / `params` —— 保持现状。
- **向后兼容**：旧工作流 `config` 里存的是单个 `model_config_id`。runner 读不到 `judge_model_ids` 时退回 `[model_config_id]`、`pass_k=1`。理由：这是真实存量数据（已保存的 graph_json），非投机防御。

### 判定逻辑（`nodes.py`）

`run_qc_judge_row` 改造为多模型：

- 入参由单个 `mc` 改为 `mcs: list[ModelConfig]` + `pass_k: int`。
- 一行并发跑 N 个判定模型（共用渲染后的 system/user 提示词，均强制 `json_mode`，解析 `{"pass","reason"}`）。
- 通过数 `n_pass = sum(pass for each judge)`；`ok = n_pass >= pass_k`。
- 失败行的 `_qc_reason` = **聚合所有「判不通过」模型的理由**（join 成多行文本），供回扫重生时拿到完整异议。
- 返回 `(ok, reason, usage, per_model)`，其中 `per_model = [{"model_config_id","pass","reason"}, ...]`（供失败样本落库展示）。usage 汇总 N 次调用。

### runner（`_run_qc_node`）

- `model_config_id` 单查改为按 `judge_model_ids`（含兼容回退）批量 `get(ModelConfig, id)`，逐个校验 `user_id == user_id`，任一不存在/不属当前用户即 `ValueError`。
- 并发判定、拆分通过/失败、失败行经 `rescan` 回扫边重生、`max_rounds` 折叠 token、最终通过行持久化在 row_idx 0 —— 流程不变。
- **首轮指标**：首次 `judge_all(inputs)` 后记录 `total=len(inputs)`、`first_round_pass=len(passed)`，写入新表 `QcMetric`。
- **失败样本**：所有轮次结束后仍 `failed` 的样本，连同最后一次判定的 `per_model` 理由，写入新表 `QcFailure`。

### 新数据表（`models.py`，`create_all` 自动建，无迁移痛点）

```python
class QcMetric(Base):
    __tablename__ = "qc_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    total: Mapped[int] = mapped_column(default=0)
    first_round_pass: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class QcFailure(Base):
    __tablename__ = "qc_failures"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    sample_json: Mapped[str] = mapped_column(Text, default="")     # 失败样本（去掉 _qc_reason）
    reasons_json: Mapped[str] = mapped_column(Text, default="")    # per_model 理由列表
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

**首轮质检通过率 = `first_round_pass / total`**（首次判定即通过、不计回扫；多模型时「通过」=≥K 个模型通过）。这正是 F3 优化要拉高的客观量。工作流含多个 QC 节点时，F3 取**全部 QC 节点聚合**：`sum(first_round_pass) / sum(total)`。

### API

- `GET /api/runs/{run_id}/qc-failures` —— 归属校验后返回 `[{node_id, sample, reasons, created_at}]`。
- `GET /api/runs/{run_id}/qc-metrics` —— 返回 `[{node_id, total, first_round_pass, first_round_rate}]`，供 UI 与 F3 客观判定。

### 级联清理

`delete_run` / `delete_user` 的级联删除补上 `QcMetric`、`QcFailure`（与现有 RunLog/RunRow 同样处理，避免孤儿）。

### 前端 / CLI

- `QcForm`：判定模型从单选改多选（`judge_model_ids`）+ 「至少通过数 K」输入框。
- 运行详情页：新增「质检失败样本」区，表格展示样本 + 各模型理由，可下载（blob，复用 RunLog 下载模式）。
- `gf node set`：QC 节点支持 `judge_model_ids=1,2,3` 与 `pass_k=2`（CLI 解析逗号分隔）。
- `api/types.ts`：新增 `QcFailureEntry`、`QcMetricEntry`。

---

## F2 · 上下文 Compactor（共享模块）

### 目标

独立 LLM 压缩模块，把过长的 Agent 工作历史压成更短的等价历史，省 token、不丢目标。goal_mode 与其他 Agent 角色统一复用。

### 新模块 `app/agent/compactor.py`

核心函数（pydantic-ai history processor 形态）：

```python
async def compact(messages: list[ModelMessage], *, compactor_model, window: int,
                  emit=None) -> list[ModelMessage]:
    if estimate_tokens(messages) < 0.75 * window:
        return messages                       # 未达阈值，原样返回
    # 1. 首尾保护：保留第一条用户消息（目标）+ 最近 KEEP_TAIL 条
    # 2. 中间段：剥离工具输出至「只留执行结果」，交 compactor LLM 凝练
    # 3. 产出结构化「已完成 / 待完成」摘要，作为一条合成消息插回中段位置
    if emit: await emit("compacting", {...})
    ...
    return head + [summary_msg] + tail
```

- `estimate_tokens`：字符启发式（不引 tiktoken 依赖）。
- 压缩规则（你给的三条）：
  1. **工具输出只留执行结果** —— 剥离中间噪声/重复内容。
  2. **首轮尾轮保护** —— 第一条用户消息（目标）+ 最近 `KEEP_TAIL` 条逐字保留，绝不丢目标。
  3. **逻辑清晰** —— compactor LLM 把中段凝练成明确的「已完成 / 待完成」结构化摘要。

### 集成点：pydantic-ai `history_processor`

在 `factory.create_agent` 统一挂载，所有角色（coordinator / manager / worker）自动复用：

```python
def create_agent(model, tools, instructions, *, compactor=None) -> Agent:
    processors = [compactor] if compactor else None
    return Agent(model, toolsets=[...], instructions=instructions,
                 history_processors=processors)
```

- `compactor` 是一个已绑定好「compactor 模型 + 当前运行模型的窗口 + emit」的可调用对象（`partial(compact, ...)`）。
- 测试传入 `TestModel`/`FunctionModel` 时 `compactor=None`，跳过压缩（不影响现有测试）。

### 窗口来源：`app/agent/model_meta.py`

- 拉 OpenRouter `GET /api/v1/models`（进程内缓存），按 `model_name` 查 `context_length`。
- 查不到 / 端点不可达 → 回退默认窗口 `DEFAULT_WINDOW = 128_000`。
- 该端点公开、**不带任何 user key**（无泄漏风险）。

### 压缩模型

- `models_json` 新增 `compactor` 角色键。
- **默认复用 coordinator 模型**（用户未单独配置 compactor 时）。
- `AgentSystem` 装配时构造 `make_compactor(running_mc)` → 返回绑定该运行模型窗口 + compactor 模型的处理器；每个 `create_agent` 调用传 `compactor=make_compactor(role_mc)`。窗口取**运行中模型**的窗口（coordinator/worker 可能不同）。
- `WorkerOrchestrator` 透传 compactor 工厂到其 `create_agent` 调用。

### 原文处理

- 发给模型的工作历史就地替换为压缩版（省 token）。
- 完整原文仍在 `AgentMessage` 表，UI 展示不丢信息（聊天记录看到的是原文）。
- 压缩触发时 emit `compacting` 事件，前端提示「正在整理上下文…」。

### 容错

compactor 调用失败 → 跳过压缩、用原历史继续（绝不让压缩失败打断回合）。

---

## F3 · 目标优化模式

### 输入

- **单个文本目标框**（例「把首轮质检通过率提升到 90% 以上」）。
- 系统从文本抽取数值阈值（百分比正则）；指标默认 = 首轮质检通过率。
- 抽不到阈值 → 退回纯 Agent DONE + 无提升早停 + 手动停（不做系统硬判定）。
- **前置条件**：目标工作流须含 QC 节点，否则启动即报错说明（否则首轮质检通过率无定义）。

### 循环（简单 while-True，turn manager 内，循环驱动、Agent 只负责改）

控制流采用「**循环触发跑数并 await**」（而非 Agent 自己 `gf run`）：循环确定性拿到 run_id、精确 await 完成事件，Agent 永不阻塞、职责纯粹是「凝练经验 + 改图」。

每一轮：

1. **循环构造轮次提示**喂给 Agent：当前实测指标 + 一份**真实 QC 失败样本抽样**（含各模型理由）+ 明确指令「**凝练通用经验、不要逐条打补丁**」，然后用 gf 改工作流（提示词/参数，必要时改链路）。logs 由 Agent 按需用 gf 命令选择性读。
2. **跑 Agent 回合**改工作流（其间 Compactor 75% 自动触发）。
3. **循环触发一次跑数**（默认全量；Agent 可在提案里用标记请求样本量，未指定退回全量），通过 API 同路创建 Run 并调度 `execute_run`，拿到 run_id 后 **await 一个 `asyncio.Event` 等跑完**（事件驱动、静默、零 token；跑完由 run manager 在 `_finish`/`execute_run` 末尾置位该事件「推送」唤醒循环）。
4. **跑完算客观指标**（读 `QcMetric` 算 first_round_rate），记录本轮指标 + 该轮 run_id（其 WorkflowVersion = 回滚点）。
5. **跳出判定**（满足任一即停）：
   - 实测 ≥ 阈值 → **成功**；
   - Agent 主动发 `DONE` 标记 → **提前结束**（如判定不可达）；
   - 连续 `K` 轮无提升（实测指标未超过历史最佳）→ **早停**（默认 K=2；仅在有结构化指标时生效）；
   - 用户手动停（复用现有 stop_flags）。
   - `agent_goal_max_rounds`（默认 20）保留为**静默最终兜底**，不作主停条件。

### 事件驱动跑数等待

- 新增进程内 `run_waiters: dict[int, asyncio.Event]`（或复用 run manager 的完成回调）。
- runner 在运行终态（completed/failed/cancelled）置位对应 run_id 的 Event。
- 循环 `await event.wait()` 期间不发任何 LLM 请求（静默、零 token）。

### 快照 / 回滚

- 复用运行版本快照：每轮跑数本就把工作流图快照成 `WorkflowVersion`。
- `gf wf restore <run>`：把指定运行的 WorkflowVersion 图恢复进当前工作流（新 CLI 命令 + 对应 API）。
- 运行详情页「恢复此版本」按钮。
- 循环全程跟踪 best-so-far（最高指标对应的 run_id）。

### UI 展示（RedLotus 抽屉内）

- 抽屉新增「目标」启动区：单文本框 + 启动按钮。
- 启动后展示：每轮指标趋势、Agent 本轮改了什么、停止按钮。
- 复用现有 `goal_round` 事件 + 新增每轮 `goal_metric` 事件（携带 round、metric、run_id）。

### 配置 / 端点

- `POST /api/agent/sessions/{id}/goal`（或扩展现有提交入口）：入参 `{goal_text}`，启动目标优化循环。
- `config.py`：`goal_no_improve_k: int = 2`（无提升早停轮数）。
- 指标/失败样本 API 复用 F1。

---

## 数据流

```
用户文本目标
  → 解析阈值 + 指标=首轮质检通过率
  → while True:
      循环构造轮次提示(指标 + 失败样本抽样 + 凝练经验指令)
        → Agent 回合(改工作流, Compactor 75% 自动压缩)
        → 循环触发跑数 → await 完成事件(静默)
        → 读 QcMetric 算指标 → 记录(round, metric, run_id)
        → 跳出判定(阈值/DONE/无提升/手动)
  → 结束: 报告 + best-so-far + 可回滚到任意轮
```

## 错误处理

- 跑数中途失败 → 当作一轮结果（指标不可得，把失败信息作为「经验」喂回 Agent，计入无提升计数）。
- 目标工作流无 QC 节点 → 启动拒绝，明确报错。
- OpenRouter 不可达 → 回退默认窗口，循环照常。
- compactor 失败 → 跳过压缩，用原历史继续。
- 多模型判定中某模型调用失败 → 该模型本行计为「未通过」（不阻断其他模型；遵循现有 retries）。

## 安全 / KISS

- api_key 仍 Fernet 加密、绝不进提示词/日志（失败样本 = 样本数据 + 模型理由文本，无 key；QcFailure.reasons_json 是模型对样本的判词，安全）。
- 判定 / 回扫 / 恢复 / compactor 模型均校验 `user_id`（租户隔离红线不变）。
- OpenRouter model-meta 拉取用公开端点、无 user key。
- 复用：WorkflowVersion 做快照、publish/SSE 做事件、gf 让 Agent 改图、RunLog 下载模式做失败样本下载、create_all 自动建新表。

## 测试

- **F1**：K-of-N 判定（K=1/2/N、聚合理由）；首轮指标计算；失败样本持久化 + 两个 API；级联删除含新表；向后兼容（旧 `model_config_id` 回退）。
- **F2**：`estimate_tokens` 阈值触发；三条压缩规则（工具输出剥离、首尾保护、结构化摘要）；窗口查不到回退默认；compactor 失败跳过；`create_agent` 挂载/不挂载（测试 None）。
- **F3**：阈值解析（百分比/抽不到）；无 QC 节点拒绝启动；事件驱动等待（mock 跑数完成置位 Event）；跳出判定（阈值命中/DONE/无提升早停/手动停）；`gf wf restore` 恢复图。
- 复用现有 fixtures（`client`/`auth_client`/`session_factory`），前端 `npm run build` + vitest 单测。
