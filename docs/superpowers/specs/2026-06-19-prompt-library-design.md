# 可复用提示词库（Prompt Library）设计

> spec2。承接 gf CLI 增强批（spec1, master@80f3f39）末尾的待办。
> 项目约束（全程生效）：KISS、无防御性代码；所有资源 user_id 租户隔离（硬红线，新端点 404 先于行访问）；
> API Key/Authorization 绝不进任何日志/响应/输出/提示词；测试只本地不推 origin；
> codegen/节点助手提示词里只出现列名不出现行值。

## 目标

一句话：用户可在一个**提示词库**里创建、编辑、版本化、复用提示词；工作流节点的
system/user 提示词可从库里**复制**进来或**引用**（运行时取最新版）。前端管理页、
后端数据模型+API、`gf prompt` CLI 与 `gf-prompt` 技能配套。

## 已收束的边界（brainstorm 三轮 + 改进选型）

| 维度 | 决定 |
|---|---|
| 提示词结构 | 单条文本片段：正文 body + 名称 name + 描述 description |
| 节点使用 | **复制** 与 **引用** 都支持 |
| 引用解析 | 运行时取**最新版本** |
| 引用缺失 | 整 run 报错、不起跑（启动前校验） |
| 版本 | 正文变更即自动生成新版本；可回滚（线性、无损） |
| 占位符 | 保存时抽取并记录声明的 `{{变量}}` |
| 可见性 | 每用户私有（user_id 隔离） |
| 初始数据 | 从空开始 |
| 管理页 | 左列表 + 右分栏（正文编辑 ‖ markdown 预览）+ 版本面板 |
| CLI | gf prompt 全套 + gf node prompt 引用/复制 |
| 技能 | 新建 gf-prompt 独立技能 |
| 改进① | 被引用可见性 + 删除护栏 |
| 改进② | 变量缺失提示（引用/复制进节点时） |
| 改进③ | 复制为新提示词（duplicate） |

## 架构：引用在运行启动时解析（方案 A）

`backend/app/engine/runner.py` 的 `for node in topo_order(graph)` 主循环执行节点，
`node.config` 在此可改、有 DB 会话。**在进入 topo 循环前**加一个解析阶段：

1. 扫描 graph 所有节点的 `system_prompt_ref` / `user_prompt_ref`（提示词 id）。
2. 按 user_id 加载这些提示词；**任一缺失 → 抛错，run 失败、不起跑**，错误信息点名「节点 X 引用的提示词 #N 不存在」。
3. 对每个带 ref 的槽位，把该提示词**最新版本** body 写入 `node.config["system_prompt"]` / `node.config["user_prompt"]`（覆盖该槽位的内联文本）。

引擎逐行函数（`nodes.py` 的 `run_llm_synth_row` / `run_qc_judge_row`）**完全不动**——
它们仍只读 `config.get("system_prompt")` / `config.get("user_prompt")`，保持纯净、无 DB。
「取最新版」= run 启动那一刻的最新；单个 run 内一致。

## 数据模型（`backend/app/models.py` 新增两表）

```python
class Prompt(Base):
    __tablename__ = "prompts"
    id, user_id (FK users.id, indexed)
    name: str
    description: str = ""
    created_at

class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id, prompt_id (FK prompts.id, indexed)
    version: int            # 1 起递增
    body: str               # 提示词正文
    variables_json: str     # JSON 数组，声明的 {{变量}}（去重排序）
    created_at
```

- 「当前正文」= prompt 的最新 version（`max(version)`）。
- 变量抽取复用引擎正则 `TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")`（nodes.py:118）。
- 删除 prompt 级联删其 versions（手动级联，与现有删除一致，无 migration）。
- name 不强制唯一；CLI 按名解析时重名报候选（与其它资源一致）。

## 后端 API（新 `backend/app/routers/prompts.py`，仿 `model_configs.py`）

前缀 `/api/prompts`，全部 `Depends(get_current_user)`，`_get_owned` 做 404 租户隔离。

| 方法/路径 | 行为 |
|---|---|
| `GET /api/prompts` | 列表：`[{id, name, description, latest_version, variables}]` |
| `POST /api/prompts` | body `{name, description, body}` → 建 Prompt + v1（抽变量）→ 返回详情 |
| `GET /api/prompts/{id}` | 详情：`{id, name, description, current:{version, body, variables}, versions:[{version, created_at}], used_by:[{workflow_id, workflow_name, node_id, slot}]}` |
| `PUT /api/prompts/{id}` | body `{name, description, body}` → name/description 原地改；**body 与当前最新版不同则生成新版本**（抽变量）→ 返回详情 |
| `DELETE /api/prompts/{id}` | 删 prompt + versions（不预扫引用） |
| `GET /api/prompts/{id}/versions` | `[{version, body, variables, created_at}]` |
| `POST /api/prompts/{id}/rollback` | body `{version}` → 用该版本 body+variables 生成新版本 → 返回详情 |
| `POST /api/prompts/{id}/duplicate` | body `{name?}` → 新建 Prompt（当前 body 为 v1），name = 传入或「<原名> 副本」→ 返回详情 |

**版本规则**（精确）：仅当**正文 body 变化**才追加新版本；name/description 是 prompt 级元数据，
原地修改不产生版本。回滚/复制按上表生成新版本/新提示词。

**used_by（改进①）**:`GET /{id}` 与 `DELETE` 时，扫描本用户全部 workflow 的 graph_json，
找节点 config 里 `system_prompt_ref == id` 或 `user_prompt_ref == id`，返回引用点列表/计数。
一个小 helper（在 prompts.py 或 services 里），单次遍历，不缓存。

## 引擎接入（`backend/app/engine/runner.py`）

- 节点 config 新增可选键：`system_prompt_ref` / `user_prompt_ref`（int，提示词 id）。
- topo 循环前的解析阶段（见上「架构」）：加载 → 缺失整 run 失败 → 写入最新 body。
- `nodes.py`、`columns.py` 不改（提示词只是文本，不影响列血缘）。

## 前端（`frontend/src/pages/PromptsPage.tsx` + `.test.tsx`）

- `App.tsx` 加侧栏菜单「提示词库」与路由 `/prompts`。
- 布局：左列表（名称/描述/最新版号/变量数，按名搜索；空态引导「还没有提示词，点新建」）；
  右分栏——上方 name/description 输入；中部**正文编辑区 ‖ `react-markdown` 预览**并排；
  下方**版本历史**面板（列版本，可查看任一版/回滚到任一版）；显示当前声明变量。
- 操作：新建、保存（正文变更即新版本）、删除（**改进①**:若 used_by>0 弹确认列出引用点）、
  复制为新提示词（**改进③**）。
- API 类型加到 `frontend/src/api/types.ts`，调用走 `frontend/src/api/client.ts`。

### 节点配置表单接入（`frontend/src/canvas/forms/NodeConfigForm.tsx`）

- system_prompt / user_prompt 两个提示词框旁各加「从库」控件：选一条库提示词后，可
  - **复制进来**：拉取该提示词当前 body 写入文本框，之后独立（不设 ref）。
  - **引用**：设 `<slot>_ref = prompt_id`；文本框转引用态，显示「引用：<名称>」+「解除引用」按钮。
- **改进②**:选库提示词时，拿其声明变量与该节点输入列比对，缺失的列给提示
  （复用现有 MissingCols 概念）。

## CLI（新 `backend/app/cli/commands/prompt.py`）

`gf prompt` 子命令（正文输入沿用 node prompt 的 `--file F` / `--edit` / `-` 三选一）：

| 命令 | 行为 |
|---|---|
| `gf prompt ls` | 列表 |
| `gf prompt show <id\|名>` | 当前正文 + 变量 + 版本列表 + 被引用点 |
| `gf prompt add <name> (--file\|--edit\|-) [--desc D]` | 新建（建 v1） |
| `gf prompt edit <id\|名> (--file\|--edit\|-) [--name N] [--desc D]` | 改正文（新版本）/元数据 |
| `gf prompt rm <id\|名>` | 删除 |
| `gf prompt versions <id\|名>` | 列版本 |
| `gf prompt rollback <id\|名> <version>` | 回滚（生成新版本） |
| `gf prompt dup <id\|名> [--name N]` | 复制为新提示词 |

资源解析：数字=id，否则按名精确匹配，重名报候选（与现有 `resolve` 一致）。

节点引用/复制（`backend/app/cli/commands/node.py` 扩展 `gf node prompt`）：

- `gf node prompt <node> (--system|--user) --library <prompt> [--ref|--copy]`
  - `--copy`（默认，最不意外）：拉取库提示词当前 body 写入节点内联字段。
  - `--ref`：把节点 config 的 `<slot>_ref` 设为该提示词 id。
- 原有 `gf node prompt <node> (--system|--user) (--file|--edit|-)` 写内联文本不变。

## 技能（`.claude/skills/gf-prompt/`）

- 新建 `gf-prompt` 技能：命令逐条作用表 + 关键契约——
  「正文变更才生成新版本」「引用取最新版 / 缺失整 run 报错」「ref vs copy 区别」「变量= `{{x}}` 声明」。
- `gf-cli` 总入口路由表加一行 `prompt → gf-prompt`。
- `gf-node-prompt` 补一句：`--library <prompt> [--ref|--copy]` 可引用/复制库提示词。

## 测试

- **后端**:prompts CRUD；版本（保存正文生成新版本、name 改不生成）；rollback；duplicate；
  租户隔离（他人提示词 404）；used_by 扫描准确；runner 引用解析（取最新版 + 引用缺失整 run 报错）；
  变量抽取正确。
- **CLI**:prompt ls/show/add/edit/rm/versions/rollback/dup；node prompt --library --ref/--copy。
  （沿用现有 in-process server + `cli.main([...])` + monkeypatch STATE_FILE 模式。新测试文件需 `git add -f`。）
- **前端**:PromptsPage 渲染/编辑/markdown 预览/版本回滚/删除护栏；节点表单 ref/copy + 变量缺失提示。

## 非目标（YAGNI，明确不做）

- 版本逐行 diff 视图（有版本列表+回滚足够）
- markdown 预览填样例行渲染（保留 `{{col}}` 原样）
- 标签/文件夹分类（扁平列表 + 按名搜）
- 从被引用计数跳转到具体节点（先给计数）
- 跨租户共享 / 管理员全局库
- 从现有节点抽取提示词入库
- agent 系统提示词（`backend/app/agent/prompts/*.md`）纳入本库——那是另一套，不动
