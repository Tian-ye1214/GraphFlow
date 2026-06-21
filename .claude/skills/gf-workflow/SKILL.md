---
name: gf-workflow
description: Use when 用 gf 命令搭建或修改 GraphFlow 工作流的图结构——新建/删除/重命名/恢复工作流、切换当前工作流、加删节点、连线/断线、查看图、查列血缘（{{列}}从哪来）、整图 JSON 导入导出；遇到「未选择工作流」「rescan 回扫边必须从 qc 节点出发」「节点已存在」时
---

# gf-workflow —— 工作流与图结构

前置：先 `gf login`、`gf use <工作流>`（没 `use` 报「未选择工作流」）。节点/连线命令都作用于**当前工作流**。

## 工作流管理

| 命令 | 说明 |
|---|---|
| `gf wf ls` | 列表：ID、名称、更新时间 |
| `gf wf add <名>` | 创建空工作流 |
| `gf wf rm <名\|ID>` | 删除 |
| `gf wf rename <名\|ID> <新名>` | 重命名 |
| `gf wf restore <运行ID>` | 按该运行**所属的工作流**，把它的图恢复到这次运行时保存的版本（作用对象由运行 ID 决定，与当前 `use` 选中的工作流无关） |
| `gf wf export [名\|ID] [-o 文件]` | 导出链路为自包含 .gfpkg 包（缺省导出当前工作流，缺省文件名 `<链路名>.gfpkg`） |
| `gf wf import <文件.gfpkg>` | 从 .gfpkg 导入为**一条新链路**（不覆盖当前工作流；复用优先重连模型/提示词/数据集） |
| `gf use <名\|ID>` | 设当前工作流，后续 node/op/run 默认作用于它 |
| `gf show` | 当前工作流图文本视图（节点 + 摘要 + 连线） |
| `gf cols [节点ID]` | 列血缘：各节点输入/输出列（不带参看全图；带 ID 只看该节点） |

## 节点与连线（作用于当前工作流）

| 命令 | 说明 |
|---|---|
| `gf node add <类型> [自定义ID]` | 类型：`input`/`llm`/`auto`/`output`/`qc`/`http`（也接受全名 `llm_synth`/`auto_process`/`http_fetch`）。缺省自动编号 `<全名>_<n>`；自定义 ID 重复报「节点已存在」 |
| `gf node rm <ID>` | 删节点并自动清掉相连的边 |
| `gf link <源> <目标> [--kind normal\|rescan]` | 连线，默认 `normal` 正向边；`--kind rescan` 加质检回扫边（**必须从 qc 节点出发**，否则报错）。重复检测只看 (源,目标) **不含 kind**：同一对节点不能同时挂 normal 和 rescan 边，加第二条报「连线已存在」 |
| `gf unlink <源> <目标>` | 断线，删该方向上的边（不论 normal/rescan）；不存在报错 |

⚠️ **节点自动编号一律是 `<类型全名>_<序号>`（序号从 1 起）**：`llm` → `llm_synth_1`，`auto` → `auto_process_1`，`http` → `http_fetch_1`；`input`/`output`/`qc` 的全名即简写本身，故是 `input_1`/`output_1`/`qc_1`（**带 `_1`，不是 `input`**）。后续 `node set`/`link` 要用这个全名 ID，不是你输入的简写。

## 查列血缘 `gf cols`

提示词里 `{{列名}}` 能引用哪些列，全靠上游静态推导。`gf cols` 列每个节点的「输入: …」「输出: …」，确认 `{{q}}` 真的存在再写进 prompt，避免渲染成空。

## 链路打包导入导出（export/import，.gfpkg）

```powershell
gf wf export 导出流 -o flow.gfpkg   # 导出某条链路（缺省当前工作流；缺省文件名 <链路名>.gfpkg）
gf wf import flow.gfpkg            # 从 .gfpkg 导入为一条新链路（新建，不覆盖当前工作流）
```

- `.gfpkg` 是自包含 zip（`manifest` + `datasets/*.jsonl`）：含图 + 全量数据集快照 + 提示词；模型配置**去掉 api_key**（导入后需补密钥）。
- `import` 单事务原子**新建一条链路**（不是覆盖），复用优先重连同名模型/提示词/数据集；导入后用 `gf use <新链路名>` 切过去。
- 跨环境搬运整条链路用它（旧的 `wf dump/load` 已移除，被 export/import 取代）。

## show 的连线显示

`gf show` 中正向边显示 `源 -> 目标`，质检回扫边显示 `源 ⟲回扫 目标`。配质检回扫循环（qc 节点 + rescan 边）见 **gf-node-prompt**。
