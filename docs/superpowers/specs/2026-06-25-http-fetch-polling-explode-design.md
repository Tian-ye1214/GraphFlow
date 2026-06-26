# http_fetch 轮询 + 结果展开 + 起始数据源 设计

日期：2026-06-25

## 背景与问题

`http_fetch` 节点（批 17 引入）逐行处理：渲染 endpoint/params/headers/body → 调一次接口 →
按 `extract` 的 JSON 路径提取落列，每行产出**恰好一行**。两处局限：

1. **只取一次**：`run_http_fetch_row`（`nodes.py:229`）调 `http.fetch(...)` 一次即返回。
   `http.fetch`（`services/http.py:24`）虽有 `retries`，但只在**传输层失败**（HTTP ≥400 / 网络错）时重试；
   一个返回 `200 {"status":"pending"}` 的响应被当成成功原样返回。**无法等待异步任务完成**。
2. **结果不展开**：`extract` 命中一个数组时，整数组塞进单个单元格；无法把「一次取回的 N 条记录」
   摊成 N 行喂给下游逐行处理。

用户场景：调一个**异步/长任务接口**（POST 起任务 → 轮询同一地址直到 `status` 就绪），就绪后响应里含一个
**大记录数组（如 10000 条）**，希望把它当**数据输入**——展开成 N 行流过链路（含质检）→ 输出。
此时「这不是一行，是一批数据」。

## 用户确定的边界（来自两轮澄清）

- **轮询形态**：同一请求反复重试（poll-in-place），非「提交后轮询另一个状态地址」的两段式。
- **完成判定**：响应里某个 JSON 字段达到期望值（如 `status == "completed"`）即停。
- **轮询控制 = 两个用户配置参数**：`poll_interval`（间隔秒，例 10）+ `poll_max_attempts`（次数上限，例 100）。
  耗尽次数仍未完成 → **取数失败、点名节点**（不早退于「失败状态」，不按总时长封顶——这两个想法已砍）。
- **结果展开**：完成后把记录数组展开成 N 行（数据源行为）。
- **节点角色**：**两者都要**——既能当工作流起始数据源（图里无 input 节点），也能逐行取数（现有用法）。
- **超时归宿**：取数失败、点名节点（作起始数据源→该 run failed 点名；作逐行节点→该行 failed）。
- `retries`（单次请求传输层重试）与 `poll_max_attempts`（整体轮询轮次）**保持分开**，语义不同不混用。

## 架构现状（约束 / 复用点）

- `run_http_fetch_row(config, row)` 是唯一的取数逻辑，仅 runner 三处调用（`_run_http_node`、生成循环 http 分支）；
  无 node-assist / CLI 预览直接调它。
- 逐行隔离脚手架 `_run_per_row_node`（`runner.py:349`）已提供：断点续跑、节点内并发、硬中断、
  逐行成败计数与落库、收尾置态。`row_coro` 返回 `(out_rows, usage)`，**`out_rows` 本就可为多行**
  （fanout 即「一个 row_idx 下挂 N 行」）→ 展开天然复用此机制。
- 硬中断 `_cancellable`（`runner.py:29`）：取消时中止在途协程（含 `await asyncio.sleep`）→ 行保持 pending、
  resume 重轮。**轮询的取消语义白嫖此机制，无需新代码。**
- `attach_child_trace`（`trace.py:56`）：`out_rows` 长度 >1 且有父 trace 时，自动给每行分配唯一子 trace
  （`make_child_trace_id`）→ 展开的 N 行 trace 不撞。
- 列血缘 `column_outputs`（`columns.py:67`）：http_fetch 输出列 = `输入列 ∪ extract.keys`。
- 起始节点路由：`_generation_chain`（`runner.py:472`）当前把无入边的 `llm_synth`/`http_fetch` 都判为
  「无输入生成链」→ 走 `_run_generation_loop`（反复分批生成直到 `output.count`）。
- `validate_graph` **不要求 input 节点**；topo 模式下无父节点的 http_fetch 现在拿到 `inputs=[]` → 0 行 no-op。
- `_run_barrier_node` 的 output 分支已在 topo 模式按 `count` 截断（`runner.py:330`）。
- 测试约定：monkeypatch `app.services.http.fetch` 注入 fake，见 `tests/test_http_node.py`。
- 现有 `test_gen_loop.py`、`tools/inputless_gen_live.py` **仅用 llm_synth 作生成起点**，无任何测试/脚本
  以 http_fetch 作生成起点 → 改动 http 起始路由风险低。

## 设计

三个能力**彼此独立、可任意组合**：可只轮询不展开（异步任务返回单个结果对象）、只展开不轮询
（同步接口直接返回数组）、或两者都用（长任务返回大数组，主场景）。

### 1. 新增 config 键

```
poll_status_path  : str   响应里状态字段的 JSON 路径，如 "status" 或 "data.state"。设了(非空)即开启轮询
poll_until        : str   期望的「完成」值，如 "completed"。字符串归一比较。开轮询时必填
poll_interval     : num   轮询间隔秒，默认 2
poll_max_attempts : int   轮询次数上限，默认 30。耗尽仍未完成 → 抛错(取数失败、点名节点)
records_path      : str   (可选)记录数组的 JSON 路径。设了即展开：每元素一行，extract 相对元素取值
```

- 开启原则**沿用代码库「presence-based」惯例**（`count` 空 = 不限、`records_path` 空 = 不展开）：
  `poll_status_path` 非空即开轮询；`records_path` 非空即展开。
- 列血缘**不变**：输出列恒为 `输入列 ∪ extract.keys`（展开只改行数不改列），与「自声明产出列」哲学一致。
- 对照表（写进 UI 与文档，避免两个「次数」混淆）：
  | 参数 | 管什么 |
  |---|---|
  | `retries`（已有） | 单次请求传输层失败（5xx/断网）重试几次 |
  | `poll_max_attempts`（新） | 整体轮询状态未就绪时重发几轮 |

### 2. 轮询循环（在 `run_http_fetch_row` 内编排，复用 `http.fetch` 单次原语）

`http.fetch` 保持单次请求原语不动；轮询的「状态判定」属节点域知识（`json_path_get`），放 `nodes.py`。

```
渲染 method/endpoint/params/headers/body（与现行一致，循环外算一次）
polling = bool(poll_status_path)
for attempt in range(poll_max_attempts if polling else 1):
    status, text = await http.fetch(...)        # 内部仍按 retries 处理传输层失败
    try:
        data = json.loads(text, parse_constant=lambda _v: None)
    except (ValueError, TypeError):
        data = None                              # 非 JSON：轮询时视为「未就绪」继续；非轮询时按现行抛错
    if not polling:
        if data is None: raise ValueError("接口响应非 JSON，无法提取…")
        break                                    # 现行单次行为，完全不变
    if data is not None and str(json_path_get(data, poll_status_path)) == str(poll_until):
        break                                    # 完成
    if attempt < poll_max_attempts - 1:
        await asyncio.sleep(poll_interval)       # cancel 期间 _cancellable 中止 → 行 pending、resume 重轮
else:
    raise ValueError(f"轮询 {poll_max_attempts} 次仍未达完成状态 '{poll_until}'（…endpoint…）")
# 到此 data 必为已完成的合法 JSON
```

- 第 1 次立即发；未完成才 `sleep` 后再发；最多 `poll_max_attempts` 次，N 次之间 N−1 次等待。
- 同步接口（`poll_status_path` 未配）→ 只发一次、非 JSON 即抛错 → 与现有行为**逐字节一致**。
- 轮询时非 JSON 或状态路径缺失 → 当「未就绪」继续轮询（兜底健壮，最终靠次数上限收口）。
- 错误文案只含 method/endpoint/status，**不含 headers/body**（防 token 外泄，沿用 http.py 约定）。

### 3. 结果展开

完成后（`data` 已是合法 JSON）：

```
if records_path:
    arr = json_path_get(data, records_path)
    if not isinstance(arr, list):
        raise ValueError(f"records_path '{records_path}' 未指向数组（实际 {type}）")  # 点名 → 行/run failed
    out_rows = [{**base, **{col: norm(json_path_get(el, p)) for col, p in extract.items()}} for el in arr]
    # 空数组 → out_rows = []（合法：本次取数无记录）
else:
    out_rows = [{**base, **{col: norm(json_path_get(data, p)) for col, p in extract.items()}}]  # 现行单行
```

- `norm(v)` = `"" if v is None else v`（字段缺失→空串，非缺失保原类型；与现行一致）。
- 展开 N 行复用 fanout 的「一个 row_idx 下挂 N 行」+ `attach_child_trace`（N>1 自动分配子 trace）。
- `extract` 路径在展开模式下**相对每个元素**取值；非展开模式相对整个响应（语义清晰、列声明式、血缘准）。

### 4. 起始数据源角色（唯一的结构性改动）

- `_generation_chain` 的 `gen_types` **去掉 `http_fetch`**（只留 `{"llm_synth"}`）→ 无入边的 http 节点
  不再走生成循环，落到普通单遍 topo。
- `_node_inputs`（`runner.py:205`）：当节点 `type == "http_fetch"` 且**无上游父节点**时，返回一个带 root trace
  的空种子 `[{}]`（`attach_root_trace([{}], run_id=…, node_id=…)`），使其**恰好触发一次**取数；
  其余无父节点仍返回 `[]`（不变）。
- 之后 §2/§3 轮询+展开出的 N 行经 topo 正常流过下游；output 节点按 `count`（若设）截断。
- `_run_generation_loop` 删去 http 分支（去掉 `gen_types` 的 http 后成死代码），简化为 llm-only。
- **行为变更（据实记录）**：批 22 的「http 作生成起点、在循环里反复发同一请求凑 count」被替换为
  「无输入 http = 单次取数数据源」。无测试/live 覆盖旧行为（已核），风险低；且旧行为本身可疑
  （同一请求反复发→返回同一批数据→重复），新语义更正确。

### 5. config 形状预校验（`validate_node_config_shape` 的 http 分支，沿用「脏草稿→整 run failed 点名」风格）

新增（present 时才校验类型；不符抛 `ValueError` 点名节点/键）：
- `poll_status_path`：str。
- `poll_until`：标量（落比较时 `str()`）；`poll_status_path` 非空但 `poll_until` 缺失 → 报错。
- `poll_interval`：number ≥ 0（非 bool）。
- `poll_max_attempts`：int ≥ 1（非 bool）。
- `records_path`：str。

### 6. 前端（`HttpFetchForm`，`NodeConfigForm.tsx:848`）

- 新增「轮询」折叠面板：一个开关（开 = 写入默认 `poll_status_path`/`poll_until` 占位、关 = 清空 `poll_status_path`）
  + 4 个字段（status 路径 / 完成值 / 间隔秒 / 次数上限）。文案点明「同一请求反复发直到状态就绪」。
- 「提取」面板加 `records_path` 输入框，说明「填了即把该数组展开成多行（每元素一行，下方提取路径相对每个元素）」。
- 「高级」面板的 `retries` 旁补一句区分注释：`retries` = 单次请求传输层重试，与轮询次数不同。
- `liveOutput` 的 http 分支血缘维持 `inputCols ∪ extract.keys`（不变）。

## 边界与已知取舍

- **起始节点产 N 行落进单个 RunRow**（一个取数单元 row_idx=0），进度显示 1/1 而非 N；全量进内存——
  与现有引擎设计一致，10000 量级 OK。resume 时该单元 done 即整体跳过（取数原子）。
- **逐行 × 展开的乘积**：100 个输入行各取回 10000 条 → 1M 行，全内存。属用户责任（KISS，不设隐藏上限）。
- **轮询中途 `http.fetch` 自身 `retries` 耗尽仍 5xx** → 整次取数失败（不把瞬时 5xx 当「未就绪」）。
  靠调大 `retries` 缓解。V1 不做「轮询期间容忍 HTTP 错」。
- **完成值比较**：`str(cur) == str(poll_until)` 精确（不区分大小写归一留作后续，按需再加）。
  仅支持单个完成值（多完成值/失败值早退 = YAGNI，已砍）。
- **`poll_max_attempts` 计入首次**：首次即完成 → 1 次、零等待，不浪费剩余次数。

## 测试计划（TDD）

后端（monkeypatch `app.services.http.fetch`，`poll_interval=0` 免真实等待，无需碰时钟）：

1. 轮询：fake 前 K 次返回 `{"status":"pending"}`、第 K+1 次返回 `{"status":"completed","data":…}` →
   断言发了 K+1 次、最终正确 extract。
2. 轮询超时：fake 恒返回 pending、`poll_max_attempts=3` → 断言发 3 次后抛 ValueError（含「轮询」「completed」）。
3. 轮询整 run/逐行：超时 → 起始数据源场景该 run failed 点名节点；逐行场景该行 failed、其余行不受影响。
4. 展开：`records_path` 指向 N 元素数组、`extract` 相对元素 → 产 N 行、列 = extract.keys、含 base 列。
5. 展开空数组 → 0 行（合法，非失败）。
6. 展开 `records_path` 非数组 → 抛错点名。
7. 轮询 + 展开组合：poll 到 completed 后展开大数组。
8. 起始数据源（无 input 节点）：`http_fetch → output`、`http_fetch → llm → output` → 一次取数、展开、流过下游、output 按 count 截断。
9. 起始路由不破坏现有：含 input 节点的工作流、llm_synth 生成链（`test_gen_loop.py`）全绿。
10. 脏 config 预校验：新键各非法形状 → 整 run failed 点名键。
11. 现有 11 个 http 测试 + trace/resume/合并相关全回归绿。

前端：`NodeConfigForm.test.tsx` 补轮询面板开关与 `records_path` 字段的渲染/patch 断言。

Live（重启后人工跑，沿用 `tools/` 风格、smoke 用户建即删）：真实长轮询接口（或 mock 异步任务）
端到端：起始数据源取大数组 → 展开 → 下游 LLM 逐行 → output。

## 不做（V1 范围外）

- 两段式「提交后轮询另一个状态地址」。
- 总时长封顶、失败状态早退、多完成值。
- 轮询期间容忍 HTTP 错误。
- 嵌套展开（数组里套数组）/ 通配 JSON 路径（`json_path_get` 本就不支持，沿用）。

## 实现记录（2026-06-26）

按计划 6 任务全部实现，每任务 TDD（RED→GREEN）+ 独立 subagent 评审通过，零 Critical/Important 遗留。

- **Task 1** 轮询循环 `_http_poll`（间隔 + 次数上限，首次即发、达完成值即停）。
- **Task 2** `records_path` 展开成多行（extract 相对元素，复用 fanout 多行机制；空数组→0 行；非数组→点名报错）。
- **Task 3** 新键脏 config 预校验（bool-first 守卫数值键、`poll_status_path` 有则 `poll_until` 必填）→ 整 run failed 点名键 + 节点 id。
- **Task 4** 无输入 `http_fetch` 改走 topo 作起始数据源：`_node_inputs` 喂带 root trace 的空种子 `[{}]` 触发一次取数；`gen_types` 收窄为 `{"llm_synth"}`，`http_fetch` 退出生成循环（死 http 分支已清）。
- **Task 5** 前端 `HttpFetchForm` 加「轮询」面板（状态路径/完成值/间隔/次数）+ 提取面板加 `records_path`，presence-based 无 Switch；重试字段加「与轮询次数不同」区分注释。
- **Task 6** 全量回归。

实现完全照设计，无设计偏差；已知取舍见上一节（起始节点进度 1/1、逐行×展开乘积全内存、轮询期不容忍 HTTP 错、完成值精确比较）。

**回归结果**：后端 `pytest -q` **776 passed**（新增 ~14 个 http 测试）；前端 `vitest run` **89 passed / 19 文件**、`tsc --noEmit` 干净。

**留观（非本特性引入）**：后端全量跑出 4 条 `aiosqlite`「Event loop is closed」teardown 告警——仅在整套连跑时出现（`test_http_node.py`、`test_columns.py` 单独跑均 0 告警），系 Windows 上 pytest-asyncio + aiosqlite 关闭期竞态的既有噪声，与本分支无关。

**过程备注**：`backend/tests/test_gen_loop.py` 在父提交 71fd62c 处为未跟踪文件，本分支随 Task 4 入库（diff stat 显 +530 多为既有内容，实际改动仅重命名 + 更新 1 个过时 http 测试），与其余已跟踪测试文件一致。

Live（重启后人工跑）尚未执行，待合并后线上重启验证。
