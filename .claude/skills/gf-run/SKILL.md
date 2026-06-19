---
name: gf-run
description: Use when 用 gf 命令运行 GraphFlow 工作流并检查结果——启动运行、看进度/列表、取消、重跑失败行、导出结果，或看某节点的结果行/运行日志/模型对话/质检指标与失败样本、删除运行记录；遇到运行卡住、watch 怎么退、质检失败样本怎么落 jsonl 时
---

# gf-run —— 运行与检查

前置：`run`/`watch`/`rmrun --all` 默认作用于**当前工作流**，先 `gf use`；带 `<运行ID>` 的命令对任意运行生效。

## 运行管理

| 命令 | 说明 |
|---|---|
| `gf run [-f]` | 运行当前工作流，立即返回运行 ID；`-f`/`--follow` 在终端原地刷新跟随各节点进度直到终态 |
| `gf runs` | 全部运行倒序：ID、工作流、状态、创建时间 |
| `gf watch [运行ID]` | 跟随进度；缺省取当前工作流最近一次运行 |
| `gf cancel <运行ID>` | 仅 queued/running 可取消，否则 409「不可取消」 |
| `gf rerun <运行ID>` | 重跑失败行（失败行重新排队，下游节点数据自动重算） |
| `gf export <运行ID> [-o 文件] [--format jsonl\|csv\|xlsx] [--node 节点ID]` | 导出结果。缺省取**第一个输出节点**的 done 行；默认文件名 `run<ID>.<格式>`（指定 `--node` 时 `run<ID>_<节点>.<格式>`） |
| `gf rmrun <运行ID>` / `gf rmrun --all` | 删单次运行 / 清空全部运行（二选一，都不给报用法错误退出码 2） |

⚠️ **`watch`/`run -f` 的 Ctrl+C 只退出查看，不取消运行**；要真停跑用 `gf cancel <运行ID>`。

## 检查结果

| 命令 | 说明 |
|---|---|
| `gf rows <运行ID> [--node 节点ID] [--failed] [--page N]` | 看某节点结果行（每页 20）。`--node` 缺省取**第一个输出节点**；`--failed` 看失败行（否则 done 行） |
| `gf logs <运行ID> [--model]` | 看运行日志；`--model` 改看模型对话记录（source / 节点 / 模型名） |
| `gf qc <运行ID> [--download] [-o 文件]` | 看质检指标（各 qc 节点首轮通过率）+ 失败样本；`--download` 把最终失败样本全量落 jsonl（缺省 `run<ID>_qc_failures.jsonl`） |

`gf qc` 不带 `--download` 打印每个 qc 节点「首轮通过 m/n（x%）」与失败样本（样本 + 各判定模型的 `status:reason`）。

⚠️ `gf qc --download` 的 jsonl 每行 = 样本字段 + 各判定模型平铺：`_qc_model_<i>`（该模型判定状态）、`_qc_model_<i>_reason`（理由），`<i>` 从 1 起。判定状态走枚举 `status`（`pass`/`failed`/具体分类），不是旧布尔 `pass`。

运行状态：排队中 → 运行中 → 已完成 / 失败 / 已取消；节点状态：等待 / 运行中 / 完成 / 失败（含失败行数）。
