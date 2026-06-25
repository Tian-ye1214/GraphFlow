# 无输入起始节点 + 生成到达 count 设计

日期：2026-06-25

## 背景与问题

当 `llm_synth`（或 `http_fetch`）节点作为工作流起点（无普通入边）时，运行会产生 0 个任务：
`_node_inputs` 对无父节点返回 `[]` → `_run_per_row_node` 迭代 `range(0)` → 不生成任何行，节点 `done=0`
即「完成」。

对纯生成 / mock 类数据流，用户期望**不需要 input 节点**，直接从 llm 或 http 节点生成数据。

## 用户确定的语义（不是固定种子数）

不在起始节点上加「生成行数」字段、也不从 count 推导一个固定种子数，而是一条**实时生成循环**：

- 起始节点**持续生成**；行流过链路（含质检）；**输出节点累计「被接收（通过质检）的好行」**。
- 当接收数达到输出节点配置的 `count` 即**停止生成**；输出精确截到 `count` 条。
- **质检拒绝的行不计入接收数**，循环继续生成补足 —— 原始生成量天然 > count
  （用户原话：接收数量是 10，生成肯定大于 10）。
- **不设预算上限**：质检长期凑不够时 run 持续 `running`，由用户在运行页**手动取消**（沿用已有硬中断）。
- **输出节点的 `count` 因此对无输入起始链是必填**。

## 架构现状（约束）

- 引擎 `_execute` 严格**单遍**：`for node in topo_order(graph)`，每节点恰跑一次（synth 跑完 → qc → output）。
- runner 内**没有** loop-until-target 机制；「目标模式」在 Agent 层（RedLotus）循环整 run，不在引擎。
- 既往「流式/循环」被否决的原因是 **merge barrier**（多父合并需全部上游就绪）——但**纯线性生成链
  （无合并）不触发该问题**，故对线性链做循环是可行且干净的。
- `validate_graph` 不要求 input 节点；前端 `run()` 直接 POST、`onConnect` 不限制连线；
  llm/http 节点本就有 source handle。所以**没有任何校验阻断**无输入起始节点被保存/运行——
  问题纯在运行时 0 任务。

## 设计

### 1. 触发与校验（run 启动时，`_execute` 内）

- 检测**无输入起始生成节点** = 类型 ∈ {`llm_synth`, `http_fetch`} 且无普通入边的节点。
- 存在则走**新增循环执行路径**；否则走现有单遍 topo（**完全不变**）。
- 校验（不符 → 整 run failed 并点名节点，沿用 `ValueError` → `execute_run` 落 `run.failed`）：
  1. 整图为**单一线性链**：无 input 节点、无分叉、无合并、单一无输入起点、终点唯一。
     （V1 范围；混用 input 节点 / 多起点 / 分叉合并 → 明确报错。）
  2. 终点为唯一 `output` 节点且 `count` 为 ≥1 整数 —— 缺失/非法报
     「无输入起始的生成链必须在输出节点设置接收数量（count）」。
- 链的合法形状：`start(llm/http) →（可选 auto_process）→（可选 qc）→ output(count)`，
  每节点单父单子（与 `_resolve_output_count` 已走的直链同形）。

### 2. 执行循环（复用现有节点函数）

伪代码：

```
accepted = []                         # 累计「到达 output 前」的好行
batch_no = 0
while len(accepted) < count and not cancel_event.is_set():
    gap = count - len(accepted)
    seeds = make_seeds(gap, fanout, observed_yield)   # 空行 {} + root trace；批量自适应
    rows = seeds
    for node in chain_before_output:                  # start →(auto_process)→ qc，逐节点处理本批
        rows = run_node_batch(node, rows, row_offset)  # 复用既有节点执行函数，传入「批 + row_idx 偏移」，返回产出
    accepted.extend(rows)
    batch_no += 1
write_output(output_node, accepted[:count])           # output 跑一次，截到 count
```

- **种子行**：`{}` 空 dict，经 `attach_root_trace(rows, run_id, node_id=start_id)` 获得 root trace
  （与 input 节点语义一致，起始生成节点即「根」）。`run_llm_synth_row` / `run_http_fetch_row` 对空
  输入行已天然支持（`base = strip_internal(row) = {}`，模板无替换，fanout 照常）。
- **复用**：`_run_llm_node` / `_run_qc_node` / `_run_barrier_node` 本就接收 `inputs` 参数；
  仅新增**可选 `row_offset`（默认 0，正常路径行为不变）**并让其**返回产出行**供链式衔接。
  正常单遍运行 = 偏移 0、单批的特例。逐行隔离、fanout、质检 K-of-N + 回扫、trace、模型日志、
  节点内并发、硬中断全部沿用，**零重写**。
- **批量自适应**：`seeds = ceil(gap / fanout / clamp(yield, 0.2, 1.0))`，
  `yield = 累计接收 / 累计生成候选`（首批前默认 1.0）。按已观测通过率放大缺口、上界 5×（clamp 下限 0.2）
  防单批暴量；多数情况几轮内收敛。http_fetch 无 fanout（恒 1 行/次），`fanout` 视为 1。
- 循环路径**不传** `max_output_rows`（产量由循环本身控制，避免与早停语义打架）。

### 3. 质检 / 失败 / 收尾

- **被接收 = 到达 output 前一节点且 status=done 的行**。无 qc 节点时 = synth/process 成功行；
  有 qc 时 = 通过判定的行。
- **生成失败**（LLM 调用出错/空）的行不到达 output → 不计接收 → 循环自然补足。
- **质检拒绝**的行不计接收 → 循环继续生成（这是「考虑质检、生成 > count」的来源）。
  质检节点自身的 rescan 回扫在批内照常工作。
- **收尾**：`len(accepted) >= count` → 停止生成 → output 截到 `count`。
  无自动预算上限；用户手动取消 → 硬中断即停、已落库的接收行保留、run 记 `cancelled`。

### 4. 进度 / 持久化 / 续跑

- **进度（SSE）**：output 节点 `total=count, done=接收数`，用户实时看着逼近 count；
  start / qc 节点 `total/done` 随批次**累加**（生成总量增长）。
- **持久化**：每批 synth/qc 行照常落 `RunRow`（按 `row_offset` 不撞 `(run_id,node_id,row_idx)` 唯一键）；
  失败样本落 `QcFailure`；`QcMetric` 每批一条 —— `first_round_rate` 既有实现按
  `sum(first_round_pass)/sum(total)` 聚合，故目标模式标尺天然正确累计。
  output 最终一次写 `accepted[:count]`（含 `save_as_dataset` 落数据集，与现有 output 逻辑一致）。
- **权衡**：高拒绝率长跑会让 `RunRow` 随生成总量增长——这是「不设预算」的代价，靠手动取消收敛。
  （V1 接受；后续可做「只留接收行 + 失败采样」优化。）
- **续跑**：崩溃-resume 时读已落的接收行数与各节点已落批次，从缺口续生成
  （沿用现有 done/failed 跳过语义，按确定性偏移定位批次）。QcMetric resume 去重沿用现有
  「重跑前清本节点指标」策略，按批重建。

### 5. 前端

- 输出节点 `count` 字段已存在（产量上限语义），**无需新字段**。
- 补提示：起始节点无输入时，输出 `count` 为必填（缺失时引擎已报错；可在表单加一行说明）。
- `llm_synth` / `http_fetch` 已可作起点（有 source handle、连线无限制），**无需改连线规则**。

### 6. 测试（隔离假模型 + 真实活体）

隔离假模型：
- `synth → output(count=N)` 无质检 → 恰好 N 条接收、output N 条。
- `synth → qc → output(count=N)`，假质检拒一半 → 生成 ≈ 2N、接收 = N、output N 条。
- 批次自适应：低通过率（如 0.2）下收敛轮数有界、最终达 N。
- 循环中途 `cancel_event` 置位 → 即停、已接收行保留、run=cancelled。
- `http_fetch` 起始变体（假 http 返回 JSON）→ 达 N。
- 缺 `count` / 非法 count → run failed 点名；非线性（分叉/合并/含 input）→ run failed 点名。
- 正常含 input 工作流回归：循环路径不被误触发，单遍 topo 行为字节级不变。

真实 DeepSeek（zrs）活体：纯生成 + 质检凑数闭环跑通；建即删、回基线。

### 7. 范围与权衡

- **V1 范围**：单一线性无输入生成链 `start →(auto_process)→(qc)→ output(count)`。
  分叉 / 合并 / 多 output / input 与 input-less 混用 → 暂不支持（明确报错点名）。
- `auto_process` 在链中允许，但**跨批去重 / shuffle 仅批内生效**（注明限制）。
- 批量自适应公式细节（clamp 边界、首批大小）在实现计划里定稿；本设计锁定「自适应≈缺口、按产率放大、有界」。
- 不设自动预算上限是用户明确选择；运行可观测（进度逼近 count + 失败样本）让用户判断是否手动取消。

## 审查后修订与已知限制（多 agent 对抗审查 2026-06-25）

实现后做了 5 维对抗审查（11 条确认），已修 5 项、留观 2 项：

已修：
- **混 input 不再报错改为走普通路径**：原设计「无输入生成链与 input 节点混用 → 报错」会让正常 input 工作流里
  一个编辑遗留的游离生成节点把整 run 误判失败（回归）。改为：图含 input 节点即返回 None 走单遍 topo，
  游离生成节点按既有 0 行 no-op 处理。生成链模式仅在「无任何 input 节点」时触发。
- **生成循环 qc 撞列守护**：抽 `_assert_qc_cols_no_collision` 单点，单遍 QC 与生成循环两路共用——
  qc 状态/反馈列撞用户生成列即整 run failed 点名，不再静默覆盖。
- **单批上限 `_GEN_BATCH_CAP=500`**：防大 count 一次性建 count 个种子+协程致 OOM/事件循环停摆；
  只钳单批、不钳总量（循环跨批继续逼近 count，与「不设预算、人工停」不冲突）。
- **进度 done≤total**：`_run_per_row_node` 的 total 改按「全表已落(done+failed)+本批 todo」算，
  生成循环跨批累加时不再 done>total（进度条>100%）；start 节点 `finalize_state=False` 不每批置 done，消除 done↔running 抖动。

留观（Minor，不影响 output 正确性，按 KISS 暂不修，记为已知限制）：
- **崩溃-resume 孤儿合成批**：崩溃恰发生在「synth 已落本批 done、qc 尚未处理该批」窗口时，resume 后该批被
  written 游标跳过、不再喂 qc——浪费一批已生成行、并短暂拉低 yield 估计（后续批自纠）。output 仍精确达 count。
- **rerun-failed 对生成链**：rerun-failed 把 failed 起始行置 pending，生成循环只前向补足、不回头处理 pending，
  这些行永久滞留（不影响 output 正确性、不影响 written 游标）。

## 不做（YAGNI）

- 不加起始节点「生成行数」字段（用户否决「推导固定种子数」）。
- 不做自动预算 / 倍数上限 / 连续零接收熔断（用户选「人工停止」）。
- 不支持分叉/合并的无输入生成（线性链之外）。
- 不引入任何 dry_run / 试跑（与项目既定约束一致）。
