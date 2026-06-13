# gf 命令完整参考

## 全局约定

- 入口：`backend/` 下 `uv run gf …`；全局安装 `cd backend; uv tool install -e .` 后任意目录直接 `gf`
- 状态文件 `~/.graphflow/cli.json`：`{server, cookie, workflow_id}`；`login` 写前两项，`use` 写第三项
- 资源指代：纯数字按 ID，否则按名字精确匹配；重名报错并列出候选 ID
- 报错：HTTP 4xx/5xx 打印后端中文 detail 到 stderr、退出码 1；argparse 用法错误退出码 2；服务器没起会提示「无法连接服务器」
- 帮助：`gf --help`、`gf <子命令> --help`（注意：`node set` 的键名表帮助里没有，见 SKILL.md）

## 认证与状态

| 命令 | 说明 |
|---|---|
| `gf login <用户名> [--server URL]` | 登录（dev 模式不存在则自动建用户），默认 `http://127.0.0.1:8000` |
| `gf st` | 显示 服务器 / 用户 / 当前工作流 |

## 工作流

| 命令 | 说明 |
|---|---|
| `gf wf ls` | 列表：ID、名称、更新时间 |
| `gf wf add <名>` | 创建空工作流 |
| `gf wf rm <名\|ID>` | 删除 |
| `gf use <名\|ID>` | 设当前工作流，后续 node/op/run 默认作用于它 |
| `gf show` | 当前工作流图文本视图（节点 + 摘要 + 连线） |

## 节点与连线（作用于当前工作流）

| 命令 | 说明 |
|---|---|
| `gf node add <类型> [自定义ID]` | 类型：`input`/`llm`/`auto`/`output`/`qc`（也接受全名 `llm_synth`/`auto_process`）。缺省自动编号 `<全名>_<n>`；自定义 ID 重复会报错 |
| `gf node set <ID> key=value …` | 一次可多个键值对；值含空格/中文用引号包整段 `"prompt=把 {{q}} 翻译成英文"`；值里可以再有 `=`（只按第一个 `=` 切分）。键名表见 SKILL.md |
| `gf node show <ID>` | 节点完整 JSON（含 position/config） |
| `gf node rm <ID>` | 删节点并自动清掉相连的边 |
| `gf link <源> <目标> [--kind rescan]` | 连线（默认 normal 正向边）；`--kind rescan` 加质检回扫边（必须从 qc 节点出发）；重复连报「连线已存在」 |
| `gf unlink <源> <目标>` | 断线；不存在报错 |

## op（自动处理节点的操作列表）

语法是**位置参数**（不是 key=value），8 种操作与生成的配置：

| 操作 | 语法 | 生成 |
|---|---|---|
| 去重 | `dedup [列1,列2]`（缺省全列） | `{op:dedup, columns}` |
| 过滤 | `filter <列> <min_len\|max_len\|contains\|not_contains\|regex> <值>` | `{op:filter, column, mode, value}`（len 类的值自动转 int） |
| 重命名 | `rename <原列> <新列>` | `{op:rename, mapping}` |
| 删列 | `drop <列1,列2>` | `{op:drop, columns}` |
| 拼接 | `concat <列1,列2> <目标列> [分隔符]` | `{op:concat, columns, target, sep}` |
| 转换 | `cast <列> <str\|int\|float>` | `{op:cast, column, to}` |
| 采样 | `sample <n>` | `{op:sample, n}` |
| 打乱 | `shuffle` | `{op:shuffle}` |

管理：`gf op ls <节点>`（带 1 起始序号）、`gf op rm <节点> <序号>`、`gf op add <节点> <操作> [参数…]`（追加到列表末尾）。

## 质检回扫（qc 节点 + rescan 边）——支持的，不要回复"做不到"

质检节点用 **LLM 语义判定**每行通过/不通过；不通过的行带着失败原因（`reason`）经 **rescan 回扫边** 回到上游 LLM 节点重新生成，最多 N 轮，仍不过则丢弃。这就是「质检不通过 → 回到 LLM 重处理」的有界循环（有向有环图）。

- 加节点：`gf node add qc`
- 配置：`gf node set <qc> model=<模型名> system=<判定系统提示词> prompt=<判定用户提示词,可用{{列名}}> max_rounds=<N> [conc=<并发>]`
  - 键与 llm_synth 相同：`model`→model_config_id，`system`→system_prompt，`prompt`→user_prompt，`conc`→concurrency，`max_rounds`→max_rounds。
  - 判定提示词必须引导模型**只输出** JSON：`{"pass": true|false, "reason": "不通过原因"}`。`pass` 为 `true` 则该行通过；`false` 时 `reason` 的内容会自动追加进回扫时上游 LLM 的 user prompt。
- 回扫边：`gf link <qc> <上游LLM> --kind rescan`（正向边仍是 `gf link <上游LLM> <qc>`）
- `gf show` 中回扫边显示为 `⟲回扫`

典型「翻译 + 质检回扫」链：`input → llm(译) → qc(LLM判定译文达标) → output`，外加 `gf link qc_1 llm_synth_1 --kind rescan`。

示例配置：
```
gf node set qc_1 model=通义 \
  system="判断以下译文是否达标，只输出JSON，格式：{\"pass\":true或false,\"reason\":\"原因\"}" \
  "prompt=原文:{{src}} 译文:{{a}}" \
  max_rounds=2 conc=4
```

## model（模型配置）

| 命令 | 说明 |
|---|---|
| `gf model ls` | ID、名、模型ID、base_url、`key:已配置/未配置`（**永不显示明文 key**） |
| `gf model add <名> --url <base_url> --model <模型ID> [--key <api_key>]` | `--key` 可省（无鉴权网关） |
| `gf model set <名\|ID> k=v …` | 键：`name=` `model=` `url=` `key=` + `temp=` `top_p=` `max_tokens=`（后三个进 default_params）。未给的字段保留原值；`key` 不给则不修改 |
| `gf model rm <名\|ID>` | 删除 |
| `gf model test <名\|ID>` | 真实发一条测试请求；失败打印错误并退出码 1 |

## data（数据集）

| 命令 | 说明 |
|---|---|
| `gf data ls` | ID、名、行数、列名 |
| `gf data up <文件> [...]` | 支持 jsonl/json/csv/xlsx，多文件一次传；数据集名 = 文件名去扩展名。⚠️ UTF-8 带 BOM 会报「Unexpected UTF-8 BOM」 |
| `gf data head <名\|ID> [n]` | 预览前 n 行（默认 5），逐行 JSON |
| `gf data rm <名\|ID>` | 删除（连文件一起删） |

## 运行管理

| 命令 | 说明 |
|---|---|
| `gf run [-f]` | 运行当前工作流，立即返回运行 ID；`-f` 在终端跟随各节点进度直到终态（ANSI 原地刷新；Ctrl+C 只退出查看，不取消运行） |
| `gf runs` | 全部运行倒序：ID、工作流、状态、创建时间 |
| `gf watch [运行ID]` | 跟随进度；缺省取当前工作流最近一次运行 |
| `gf cancel <运行ID>` | 仅 queued/running 可取消，否则 409 报「不可取消」 |
| `gf rerun <运行ID>` | 重跑失败行（失败行重新排队，其下游节点数据自动重算） |
| `gf export <运行ID> [-o 文件] [--format jsonl\|csv\|xlsx] [--node 节点ID]` | 缺省导出第一个输出节点的 done 行；默认文件名 `run<ID>.<格式>`（指定 --node 时 `run<ID>_<节点>.<格式>`） |

运行状态：排队中 → 运行中 → 已完成 / 失败 / 已取消；节点状态：等待 / 运行中 / 完成 / 失败（含失败行数）。

## 前端实时联动

CLI 的每次变更（工作流/节点/模型/数据集/运行）经 SSE 推送给同用户已打开的浏览器页面：

- 列表页（工作流/模型/数据集/运行）自动重拉
- 画布页：无未保存改动 → 静默刷新；有未保存改动 → 顶部提示条「工作流已被 CLI 修改」，不覆盖手动编辑
- 运行详情页维持自身 2 秒轮询，与 CLI 无关
