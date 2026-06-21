---
name: gf-prompt
description: Use when 用 gf 管理可复用提示词库——`prompt ls/show/add/edit/rm` 维护提示词、`prompt versions/rollback` 看版本与回滚、`prompt dup` 复制，以及把库提示词通过 `node prompt --library` 引用/复制到节点；遇到「正文变更才生成新版本」「引用取最新版/缺失整 run 报错」「ref 与 copy 区别」「声明变量（双花括号占位符）」相关疑问
---

# gf-prompt —— 可复用提示词库

提示词库是**每用户私有**的命名提示词集合，可版本化、复用到工作流节点。前置 `gf login`。

## 命令

```powershell
gf prompt ls                               # 列出：#id 名称 v最新版 变量 描述
gf prompt show <id|名>                      # 当前正文 + 声明变量 + 被引用节点
gf prompt add <名称> --file p.md [--desc 说明]   # 新建（建 v1）；正文也可 --edit / -（stdin）
gf prompt edit <id|名> --file p.md [--name 新名] [--desc 新说明]   # 改正文（正文变了才出新版本）
gf prompt versions <id|名>                  # 列所有版本：v号 创建时间 正文首行(截断)。只看历史，回看完整正文需先 rollback 或走 REST /versions
gf prompt rollback <id|名> <版本号>          # 回滚：用旧版内容生成新版本（线性无损）
gf prompt dup <id|名> [--name 新名]          # 复制为新提示词（默认名「<原名> 副本」）：从 v1 起、只拷源的当前正文(不含版本历史)，描述沿用源
gf prompt rm <id|名>                         # 删除（删后引用它的 run 会报错，见下）
```

- 正文来源三选一：`--file FILE` / `--edit`（开 $EDITOR）/ `-`（stdin），与 `node prompt` 一致。
- 资源指代：纯数字按 id，否则按名精确匹配，重名报候选（同其它资源）。

## 关键契约（照此理解，别猜）

- **版本**：只有**正文 body 变化**才追加新版本；名称/描述是元数据，原地改不出版本。回滚不覆盖历史，而是用旧内容生成新版本。
- **声明变量**：保存时自动抽取正文里的 `{{变量}}`（与节点渲染同款），`show`/`ls` 显示。提示词正文里只写列名占位符，不写行值。
- **被引用**:`show` 列出哪些工作流节点引用了它；删除被引用的提示词后，那些 run 启动解析时会**整 run 报错**（fail fast）。

## 把库提示词用到节点（gf node prompt --library）

```powershell
gf node prompt <节点> --system --library <提示词> --ref    # 引用：节点存 *_ref，运行时取最新版
gf node prompt <节点> --user   --library <提示词> --copy   # 复制：把当前正文内联进节点（默认）
```

- `--ref`：节点 config 写 `system_prompt_ref`/`user_prompt_ref`=提示词 id；**改库即影响所有引用节点**；引用缺失则整 run 报错。
- `--copy`（默认）：拉当前正文写进 `system_prompt`/`user_prompt` 内联字段，之后与库独立；并清除该槽位的引用。
- 写内联文本（`--file/--edit/-`，不带 `--library`）也会清除该槽位的引用。
- ⚠️ `--library` 与 `--file/--edit/-` 同属 `node prompt` 的**必选互斥来源组**（四选一其一）；`--ref`/`--copy` 仅在带 `--library` 时生效，不带 `--library` 写它们会被忽略。

详见 gf-node-prompt 的 `node prompt` 段。
