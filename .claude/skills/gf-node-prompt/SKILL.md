---
name: gf-node-prompt
description: Use when 用 gf 命令配置 GraphFlow 节点参数或写提示词——`node set` 设键值（模型/数据集/提示词/输出列/采样/思考/删列/质检判定）、`node show` 看配置、`node prompt` 写长提示词、自动处理节点的 `op` 操作、搭质检回扫；遇到「未知配置键」、不确定 node set 键名 / op 语法 / qc 判定 JSON 契约时
---

# gf-node-prompt —— 节点配置与提示词

前置：先 `gf use <工作流>`。所有命令作用于当前工作流的节点（节点 ID 用全名，见 gf-workflow）。

## `gf node set <ID> key=value …`（一次可多个键）

值含空格/中文用引号包整段 `"prompt=把 {{q}} 翻译成英文"`；值里可再有 `=`（只按第一个 `=` 切分）。

### 逐键作用表（猜不到的照抄这里；键是**短别名**，不是实际字段名）

| 设什么 | 键(别名) | 实际字段 | 取值示例 | 适用节点类型 |
|---|---|---|---|---|
| 数据集 | `dataset=名1,名2` | dataset_ids | `dataset=种子集` | input |
| 模型 | `model=名或ID` | model_config_id | `model=通义` | llm / qc |
| 系统提示词 | `system=` | system_prompt | `system=你是翻译` | llm / qc |
| 用户提示词 | `prompt=` | user_prompt | `"prompt=回答:{{q}}"` | llm / qc |
| 输出列 / 输出模式 | `out=` / `mode=column或json` | output_column / output_mode | `out=a` `mode=json` | llm |
| JSON 多输出列 | `outs=q_en,cat_en` | output_columns | `outs=q_en,cat_en` | llm（mode=json） |
| 扇出 / 并发 / 重试 | `fanout=` / `conc=` / `retries=` | fanout_n / concurrency / retries | `conc=4` | llm / qc / http |
| 采样参数 | `temp=` / `top_p=` / `max_tokens=` / `timeout=` / `json_mode=` | params.* | `temp=0` `json_mode=true` | llm / qc |
| 思考 | `think=on\|off` / `effort=low..max` | params.thinking_enabled / params.reasoning_effort | `think=on effort=high` | llm / qc / agent |
| 删列 | `drop=列1,列2` | drop_columns | `drop=secret` | 任意 |
| 质检状态列 | `status_col=名` | status_column | `status_col=verdict` | qc |
| 质检反馈列 | `feedback_col=名` | feedback_column | `feedback_col=fb` | qc |
| 质检判定模型 | `judge_models=名1,名2` | judge_model_ids | `judge_models=通义,深求` | qc |
| 质检 K-of-N / 轮数 | `pass_k=` / `max_rounds=` | pass_k / max_rounds | `pass_k=2 max_rounds=2` | qc |
| 输出存为数据集 | `save_as=名`（空串=关闭） | save_as_dataset + dataset_name | `save_as=结果集` | output |
| HTTP url/方法/体 | `url=` / `method=` / `body=` | url / method / body | `url=http://api/{{q}}` | http |
| HTTP 提取 | `extract=列:路径,...` | extract | `extract=temp:data.temp` | http |
| HTTP 请求头 | `headers=K1:V1,K2:V2` | headers | `headers=Authorization:Bearer x` | http |

⚠️ `concurrency=2`、`output_column=a`、`dataset_id=1` 都是**错的**（报「未知配置键」）——键是短别名：`conc=2`、`out=a`、`dataset=1`。

⚠️ 数字键自动转型：`fanout`/`conc`/`retries`/`max_tokens`/`timeout`/`pass_k`/`max_rounds` 转 int，`temp`/`top_p` 转 float，`json_mode` 取真值（true/1/yes），`think` 取真值（on/true/1/yes）。

## `gf node show <ID>`

打印节点完整 JSON（含 type/position/config），核对刚 set 的字段是否落对。

## `gf node prompt <ID> <--system|--user> <--file FILE | --edit | ->`（长提示词）

`node set` 适合短提示词；多行/长提示词用 `node prompt`，避免引号转义地狱：

```powershell
gf node prompt llm_synth_1 --user --file p.md      # 从文件读
gf node prompt llm_synth_1 --system --edit         # 打开 $EDITOR（win 默认 notepad）编辑
Get-Content p.md -Raw | gf node prompt qc_1 --user -   # 从 stdin 读（末尾 - ）
```

- 必须二选一指定写哪个字段：`--system`（system_prompt）或 `--user`（user_prompt）。
- 必须二选一指定来源：`--file FILE` / `--edit` / `-`（stdin）。

## `gf op`（自动处理节点的操作列表，位置参数不是 key=value）

```
gf op add <节点> dedup [列1,列2]      # 缺省全列去重
gf op add <节点> filter <列> <min_len|max_len|contains|not_contains|regex> <值>
gf op add <节点> rename <原列> <新列>
gf op add <节点> drop <列1,列2>
gf op add <节点> concat <列1,列2> <目标列> [分隔符]
gf op add <节点> cast <列> <str|int|float>
gf op add <节点> sample <n>
gf op add <节点> shuffle
gf op ls <节点>                       # 列出操作（1 起始序号）
gf op rm <节点> <序号>                # 按 op ls 的序号删
```

⚠️ `op add auto_process_1 dedup col=q` 是错的——写 `dedup q`。filter 的 `min_len`/`max_len` 值会自动转 int。

## 质检回扫（qc 节点 + rescan 边）——支持的，别回复"做不到"

「质检不通过 → 回到 LLM 重处理」的有界循环，用 qc 节点 + rescan 回扫边实现：

```powershell
gf node add qc
gf node set qc_1 judge_models=通义,深求 pass_k=2 "system=判断译文是否达标，只输出JSON" "prompt=原文:{{src}} 译文:{{a}}" max_rounds=2 conc=4
gf link llm_synth_1 qc_1                  # 正向边
gf link qc_1 llm_synth_1 --kind rescan    # 回扫边（必须从 qc 出发）
```

- qc 用 **LLM 逐行语义判定**，N 个 `judge_models` 共享提示词并发判定，≥`pass_k` 个通过即整行通过（K-of-N，默认 `pass_k=1`）。未配 `judge_models` 时退化用单个 `model=`。
- 判定提示词必须引导模型**只输出** JSON 契约：

  ```json
  {"status": "pass" | "failed" | "<更具体的失败分类>", "reason": "原因"}
  ```

  **只有 `status` 为 `"pass"` 算通过**（大小写不敏感），其余一律不通过。⚠️ **不要写旧的 `{"pass": true|false}` 契约**——现在判定字段是 `status` 枚举，不是布尔 `pass`。
- 不通过的行带着聚合 `reason` 经 rescan 回扫边回上游 LLM 重生成，最多 `max_rounds` 轮，仍不过则丢弃。
- qc 节点给输出行显式写两列：`status_col`（默认 `qc_status`，通过=`pass`）、`feedback_col`（默认 `qc_feedback`，通过=空串）；可用 `status_col=`/`feedback_col=` 改列名，下游可见。
- `gf show` 中回扫边显示为 `⟲回扫`。失败样本与每模型判定的查看/导出见 **gf-run** 的 `gf qc`（jsonl 平铺键带 `_qc_` 前缀，如 `_qc_model_1`）。
