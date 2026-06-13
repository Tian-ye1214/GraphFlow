---
name: gf-cli
description: Use when 需要用命令行操作 GraphFlow（gf 命令）——搭建/修改工作流、节点连线、配置模型、上传数据集、运行任务与导出结果，或遇到 gf 报「未知配置键」「未选择工作流」、不确定 node set 键名 / op 语法时
---

# gf —— GraphFlow 命令行

## Overview

`gf` 是 GraphFlow 的瘦 HTTP 客户端，与前端同权限同校验；每次变更经 SSE 实时反映到已打开的浏览器页面。在 `backend/` 目录下 `uv run gf …` 使用，或 `cd backend; uv tool install -e .` 装成全局命令。

**核心流程**：`login` → `use <工作流>` → 节点/连线/配置 → `run -f`。没 `use` 就执行节点命令会报「未选择工作流」。

## 快速上手

```powershell
uv run gf login alice                  # 默认 http://127.0.0.1:8000，--server 可改
uv run gf wf add 流程; uv run gf use 流程
uv run gf node add input               # 自动编号 input_1
uv run gf node set input_1 dataset=种子集
uv run gf node add llm                 # 注意：编号是 llm_synth_1（全名前缀）
uv run gf node set llm_synth_1 model=通义 "prompt=回答:{{q}}" out=a conc=4
uv run gf node add output
uv run gf link input_1 llm_synth_1; uv run gf link llm_synth_1 output_1
uv run gf show                         # 文本视图核对全图
uv run gf run -f                       # 运行并跟随进度
uv run gf export 1 --format jsonl
```

## 服务器没起？

```powershell
cd backend
uv run uvicorn app.main:app --port 8000      # 生产式（同时托管前端页面）
# 或热更新开发模式: uv run fastapi dev app/main.py
```

- 自定义数据目录：启动前 `$env:GRAPHFLOW_DATA_DIR = "D:\gfdata"`——必须**与启动命令在同一会话/同一次调用里设**；`Start-Process`/`Start-Job` 不继承其他调用的 `$env:`，后台起服务时把赋值和启动写在同一条命令里。
- 后台启动最省事用 bash 语法一行：`GRAPHFLOW_DATA_DIR="D:\gfdata" uv run uvicorn app.main:app --port 8000 &`（在 backend/ 下），起没起用 `curl http://127.0.0.1:8000/docs` 验证。
- gf 报「无法连接服务器」= 服务器没起，或 `gf login --server` 地址不对（看 `gf st`）。

## node set 键名表（猜不到的，照抄这里）

| 要设什么 | 键 | 实际字段 |
|---|---|---|
| 数据集（input 节点） | `dataset=名1,名2` | dataset_ids |
| 模型（llm 节点） | `model=名或ID` | model_config_id |
| 系统/用户提示词 | `system=` / `prompt=` | system_prompt / user_prompt |
| 输出列 / 输出模式 | `out=` / `mode=column或json` | output_column / output_mode |
| 扇出 / 并发 / 重试 | `fanout=` `conc=` `retries=` | fanout_n / concurrency / retries |
| 采样参数 | `temp=` `top_p=` `max_tokens=` `timeout=` `json_mode=` | params.* |
| 输出节点存为数据集 | `save_as=数据集名`（空串=关闭） | save_as_dataset + dataset_name |
| 质检节点 qc | `qc_col=列` `qc_mode=模式` `qc_value=值` `max_rounds=N` `reason=文案` `reason_field=列` | condition.{column,mode,value} / max_rounds / reason / reason_field |

⚠️ `concurrency=2`、`output_column=a`、`dataset_id=1` 都是**错的**（报「未知配置键」）——键是短别名：`conc=2`、`out=a`、`dataset=1`。

## op 语法（位置参数，不是 key=value）

```
gf op add <节点> dedup [列1,列2]      # 缺省全列去重
gf op add <节点> filter <列> <min_len|max_len|contains|not_contains|regex> <值>
gf op add <节点> rename <原列> <新列>
gf op add <节点> drop <列1,列2>
gf op add <节点> concat <列1,列2> <目标列> [分隔符]
gf op add <节点> cast <列> <str|int|float>
gf op add <节点> sample <n>
gf op add <节点> shuffle
gf op ls <节点>  /  gf op rm <节点> <序号>   # 序号 1 起始，见 op ls
```

⚠️ `op add auto_process_1 dedup col=q` 是错的——写 `dedup q`。

## 质检回扫（支持，别回复"做不到"）

「质检不通过 → 回到 LLM 重处理」的有界循环用 qc 节点 + rescan 回扫边实现：

```
gf node add qc
gf node set qc_1 qc_col=a qc_mode=min_len qc_value=3 max_rounds=2 reason=译文太短
gf link llm_synth_1 qc_1                 # 正向边
gf link qc_1 llm_synth_1 --kind rescan   # 回扫边（必须从 qc 出发）
```

不通过的行带着失败原因回到上游 LLM 重新生成，最多 `max_rounds` 轮，仍不过则丢弃。qc_mode 可选 `min_len/max_len/contains/not_contains/regex/not_empty/equals`。语义质检：qc 前放一个 LLM 节点输出「合格/原因」列，qc 用 `qc_col=合格 qc_mode=equals qc_value=是 reason_field=原因`。详见 reference.md。

## 常见坑

- **资源指代**：纯数字按 ID，否则按名字精确匹配；重名会报错并列出候选 ID。
- **PowerShell 写 jsonl 别带 BOM**：`Out-File`/`WriteAllText` 默认带 BOM，上传报 `Unexpected UTF-8 BOM`。用 `[IO.File]::WriteAllText($p, $s, [Text.UTF8Encoding]::new($false))`。
- **节点自动编号用类型全名**：`llm` → `llm_synth_1`，`auto` → `auto_process_1`。
- **退出码**：业务错误 1（打印后端中文 detail），参数用法错误 2，Ctrl+C 130。
- **换用户 login 不清当前工作流**：必要时重新 `gf use`。
- **watch 的 Ctrl+C 只退出查看**，不取消运行；取消用 `gf cancel <运行ID>`。

## 更多

- 全命令清单（model/data/runs/cancel/rerun/export 细节、状态文件、前端联动）：见本目录 `reference.md`
- 端到端示例脚本：`scripts/build-pipeline.ps1`
