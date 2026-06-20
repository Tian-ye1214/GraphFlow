# 链路导入导出（.gfpkg 可移植包）设计

**日期**：2026-06-20
**目标**：把一条完整链路（工作流图 + 它引用到的全量数据集、模型配置、提示词）打包成一个**自包含、可移植、可识别**的 `.gfpkg` 文件，支持备份与分发；并能在任意账号/部署上安全导入还原（绝不泄露密钥、绝不跨租户）。

**架构**：`.gfpkg` = zip 容器，内含 `manifest.json`（自描述信封 + 链路图 + 资源目录）与 `datasets/*.jsonl`（全量数据行）。导出端收集链路引用到的资源、脱敏 http 密钥；导入端解压、校验、在**导入者自己账号**内"复用优先"重连引用、原子事务建链。三个面（REST API / gf CLI / Web UI）共用同一套服务层。

**技术栈**：FastAPI + SQLite(WAL) 后端、React 前端、`zipfile` + `json`（无新依赖；不引 YAML，避免隐式类型转换破坏既有的类型保真）。

## 全局约束（Global Constraints）

- **密钥红线**：模型 `api_key` 永不出包；`http_fetch.headers` 敏感头值出包前脱敏；任何日志/响应/包内容都不得含密钥。
- **租户红线**：导入一律落到导入者自己账号；引用只在导入者自有资源内解析；**绝不信任包内任何 user_id**。
- **不可信输入**：上传的 zip 视为不可信——解压前做 zip-slip 路径净化与 zip-bomb（总解压大小/压缩比）上限；manifest 形状非法一律 422，绝不 500。
- **KISS**：只做 schema v1；版本字段先占位 + 高版本拒绝；**不写迁移逻辑**（无 v2 不写防御代码）。不预防不会发生的输入。
- **类型保真**：数据行经 `data_json` 原样 round-trip（JSONL），导入不得把 `"007"`→`7`、`"false"`→`bool`、吞 `"None"`。
- **导出范围**：只导链路定义 + 它引用到的资源；**绝不含运行历史/结果**（runs / run_rows / qc_metrics / qc_failures / model_call_logs 一概不出包）。
- **原子性**：导入全成或全回滚，不留半截孤儿。
- **测试本地**：`backend/tests/` 已被 gitignore，已跟踪测试文件用 `git add -f`；绝不 stage `__pycache__`/`.pyc`/`.idea`/`.codegraph`。提交信息中文、不含 "claude"、不加 Co-Authored-By。

---

## 1. 待重映射的引用清单（实现依据，必须列全列准）

链路图节点 `config` 内对外部资源的引用，全部是**本账号内的数字 ID**，换环境即失效，导入时须 old→new 重映射：

| 引用类型 | 节点类型 | config 键 | 形状 |
|---|---|---|---|
| 模型 | `llm_synth` | `model_config_id` | int |
| 模型 | `qc` | `judge_model_ids`（旧版回退 `model_config_id`） | list[int] / int |
| 数据集 | `input` | `dataset_ids` | list[int] |
| 提示词 | `llm_synth` / `qc` | `system_prompt_ref` / `user_prompt_ref` | int |
| 密钥（脱敏，不重映射） | `http_fetch` | `headers` | dict[str,str] |

`output` 节点仅含 `dataset_name`（字符串，无引用）。回扫（rescan）目标是 `llm_synth`，其 `model_config_id` 已被上表覆盖。

---

## 2. 包格式 `.gfpkg`

### 2.1 容器布局（zip）

```
manifest.json
datasets/<dataset_id>.jsonl     # 每个被引数据集一份；行 = 该行 data_json（逐行一个 JSON 对象）
```

### 2.2 manifest.json 结构

```json
{
  "kind": "graphflow.workflow.package",
  "schema_version": 1,
  "exporter": "graphflow",
  "exported_at": "2026-06-20T12:34:56+00:00",
  "source": { "workflow_id": 42, "workflow_name": "翻译质检链路" },
  "workflow": {
    "name": "翻译质检链路",
    "graph": { "nodes": [ ... ], "edges": [ ... ] }
  },
  "models": [
    { "id": 1, "name": "deepseek-v4-pro", "model_name": "deepseek-v4-pro",
      "base_url": "https://api.example.com/v1", "provider": "openai",
      "azure_api_mode": "legacy", "api_version": "", "default_params": { "temperature": 0 } }
  ],
  "prompts": [
    { "id": 5, "name": "翻译系统提示", "description": "把中文翻成英文",
      "body": "你是翻译助手……", "variables": ["q"] }
  ],
  "datasets": [
    { "id": 3, "name": "测试集", "original_filename": "test.csv",
      "columns": ["q", "en"], "row_count": 200, "file": "datasets/3.jsonl" }
  ],
  "redactions": [
    { "node_id": "http_1", "header": "Authorization" }
  ]
}
```

**关键约定**：
- `models[].id` / `prompts[].id` / `datasets[].id` 是**导出环境的原始 ID**，与 `workflow.graph` 节点 config 里保留的引用 ID 一致；导入时据此建 old→new 映射重写 graph。
- `models[]` **绝不含 api_key**（连字段都不出现）。
- `prompts[].body` / `variables` 取该提示词**最新版**。
- `graph` 内 `http_fetch.headers` 的敏感头值已替成 `"***REDACTED***"`；`redactions[]` 记录被脱敏的 (node_id, header) 供导入端提示回填。
- `kind` + `schema_version` 是"可识别"的依据：导入端据此判定是否本系统的包、版本是否可处理。

---

## 3. 导出（Export）

### 3.1 收集
给定工作流：
1. 解析 graph，遍历节点 config，按 §1 收集 `dataset_ids` / `model_config_id` / `judge_model_ids` / `*_ref` 去重后的引用 ID 集合。
2. 在**工作流属主账号**内查这些资源（只查自己的，对不上属主的引用视为悬空、跳过且不报错——草稿态允许）。
3. 模型：导出 name/model_name/base_url/provider/azure_api_mode/api_version/default_params（**不含 api_key_enc**）。
4. 提示词：导出最新版 body + variables + name + description。
5. 数据集：导出元信息 + 把 `dataset_rows`（按 idx 升序）逐行写成 `datasets/<id>.jsonl`（每行 `data_json` 原文）。

### 3.2 http 密钥脱敏
对每个 `http_fetch` 节点的 `headers`：
- 头名按**大小写不敏感**匹配敏感名单：包含 `authorization` / `cookie` / `token` / `secret` / `key` / `password` / `auth` 之一，或精确 `x-api-key`。
- **且**头值**不含** `{{` 模板占位（纯逐行注入的 `{{col}}` 不是固化密钥，放行）。
- 命中者：值替为 `"***REDACTED***"`，并往 `redactions[]` 追加 `{node_id, header}`。
- 非敏感头（如 `Content-Type`、`Accept`）与模板值原样保留。

### 3.3 打包
信封 + graph（已脱敏）+ 资源目录写进 `manifest.json`，数据集写进 `datasets/`，zip 成字节流返回。

---

## 4. 导入（Import）

输入：上传的 zip 字节。全程在**一个事务**内，任一步失败整体回滚。

1. **解压硬化**：拒绝条目名含绝对路径或 `..`（zip-slip）；累计解压大小超上限或单条压缩比过高（zip-bomb）即 422 拒绝。
2. **读 manifest**：缺失/非 JSON/`kind != "graphflow.workflow.package"` → 422「不是 GraphFlow 链路包」。`schema_version` 高于当前支持 → 422「包版本过新，请升级 GraphFlow」（等于则放行；**不做迁移**）。结构字段缺失/类型不符 → 422。
3. **资源重连（复用优先）**，在导入者账号内：
   - **模型**：按 `name` 找既有模型 → 命中复用其 ID（既有的自带 key，能直接跑）；未命中→用包内元信息新建（**api_key 留空**），记入"待回填模型 key"。
   - **提示词**：按 `name` 找既有 → 命中复用其 ID；未命中→新建 Prompt + 一个版本（body/variables）。
   - **数据集**：按 `name` 找既有 → 命中复用其 ID；未命中→新建 Dataset，从对应 `.jsonl` 逐行 `json.loads` 建 DatasetRow（类型保真），`source="upload"`、`run_id/node_id=None`。
   - **复用优先即不撞名**：三类资源都"同名则复用、缺失才新建"，只在缺失时新建，故新建时不会与既有同名 → **资源不加后缀**。加后缀只用于**工作流名**（§4 步骤 6）。
   - **同名多义取舍**：模型/提示词/数据集的 name 在库中无唯一约束，可能有多个同名。复用时取**最新一个（id 最大）**，规则固定可预期；导入报告里如实写明复用的是哪个（带 id）。
   - 建好 old→new 三张映射表。
4. **重写 graph**：按映射改写所有 §1 引用 ID；`http_fetch.headers` 中 `"***REDACTED***"` 原样保留（占位待回填）。映射缺失的引用（理论上自包含包不会有）→ 置空 + 记入"降级草稿"提示。
5. **校验**：`validate_graph`（环/悬空边/未知类型）→ 失败 422 回滚。
6. **建工作流**：新建 Workflow（重名加后缀「(导入)」「(导入 2)」…），写入重写后的 graph。
7. **提交**事务，返回**导入报告**。

### 4.1 导入报告（响应体）

```json
{
  "workflow": { "id": 99, "name": "翻译质检链路(导入)" },
  "report": {
    "models_reused": ["deepseek-v4-pro"],
    "models_created": [],
    "models_need_key": [],
    "prompts_reused": [],
    "prompts_created": ["翻译系统提示"],
    "datasets_reused": [],
    "datasets_created": ["测试集"],
    "headers_need_refill": [ { "node_id": "http_1", "header": "Authorization" } ],
    "draft_unresolved": []
  }
}
```

Web/CLI 据此提示用户："复用了 X、新建了 Y、请到节点 Z 回填 http 头/模型密钥"。

---

## 5. 三个面

### 5.1 REST API
- `GET /api/workflows/{id}/export` → 200，`application/zip` 流，`Content-Disposition` 的 filename 由链路名净化得来（复用 `file_parse._safe_filename` 清非法/控制字符；非 ASCII 用 RFC 5987 `filename*=UTF-8''…` 编码，附 ASCII 回退 `filename=`），扩展名 `.gfpkg`；非自有 → 404。
- `POST /api/workflows/import`（multipart `file`）→ 200 `{workflow, report}`；非法包 → 422。

### 5.2 gf CLI（取代旧 `wf dump`/`wf load`）
- `gf wf export <名|ID> [-o out.gfpkg]`：默认存 `<wf名>.gfpkg`。
- `gf wf import <file.gfpkg>`：导入并打印报告（复用/新建/待回填）。
- 移除 `gf wf dump` / `gf wf load`（旧的只导 graph、且 load 覆盖当前工作流，破坏性，由新命令取代）。

### 5.3 Web UI
- 链路列表/编辑页加「导出」按钮 → 下载 `.gfpkg`。
- 「导入」按钮 → 选 zip 上传 → 成功后跳到新链路并弹出导入报告（复用/新建/待回填项）。

---

## 6. 错误与边界

| 情形 | 处理 |
|---|---|
| 导出非自有工作流 | 404 |
| 上传非 zip / zip 损坏 | 422 |
| zip-slip / zip-bomb | 422 拒绝，不解压落盘 |
| manifest 缺失/非 JSON/kind 不符 | 422「不是 GraphFlow 链路包」 |
| schema_version 高于支持 | 422「包版本过新」 |
| manifest 结构/类型非法 | 422，不 500 |
| 导入图 validate 失败（环/悬空/未知类型） | 422，整体回滚 |
| 引用资源缺失（自包含包正常不会） | 置空降级草稿 + 报告点名 |
| 新建模型 key 为空 | 报告点名"待回填"；跑前引擎本就以点名失败拦截 |
| 数据集/提示词/模型重名 | 三类一律复用既有（取 id 最大）；缺失才新建，故新建不撞名、不加后缀 |
| 工作流重名 | 新建工作流名加后缀「(导入)」「(导入 2)」… |
| 空链路（无节点）导出/导入 | 正常（空 graph 合法） |

---

## 7. Schema 版本演进

- 当前 `schema_version = 1`，常量集中定义。
- 导入：`== 1` 放行；`> 1` 拒绝；`< 1` 不存在。
- **暂不实现迁移函数**（无 v2 不写）。真出现 v2 时，在导入读 manifest 后、重连前插入 `migrate(manifest, from_version)` 一处即可。

---

## 8. 测试策略

### 8.1 单元/集成（pytest，隔离库 + 假模型）
- 导出：四类引用全收集、模型不含 key、提示词取最新版、数据集行 round-trip 保真、http 敏感头脱敏（含模板值放行、非敏感头保留）。
- 导出→导入 round-trip：同账号、跨账号（另一用户导入），断言 graph 引用被正确重写、资源复用优先、报告正确。
- 类型保真：`"007"`/`"false"`/`"None"`/含 NaN 归一 经包来回不变。
- 不可信包：非 zip、损坏 zip、zip-slip 条目、超大解压、错 kind、高版本、脏 manifest、环图 → 全部 422 且不留孤儿。
- 租户：包内伪造 user_id 被无视；导入只落导入者账号。
- 原子性：中途校验失败→无半截 workflow/dataset/prompt/model 残留。

### 8.2 活体（真实后端 + 真实 DeepSeek，建即删 smoke_ 前缀）
- 真实导出一条含 input+llm+qc 的链路 → 导入 → 跑通。
- 跨 smoke 用户导入复用既有模型（自带 key）后直接跑通。
- 收尾删除所有 smoke 资源，真实数据零损失。

---

## 9. Phase 2：压测/冲突测试报告（功能合并后执行，独立交付）

产出一份 md 报告（保存到 `docs/` 下），三块全部给**真实实测计时**（重新实测，不抄旧会话数字）：

1. **数据库**：批量写吞吐、大数据集上传耗时、WAL 高并发写争用（并发上传/并发跑）、高并发下跨租户隔离是否保持。
2. **链路并发**：单节点逐行并发（不同 concurrency 档）、多 run 同时跑的总耗时与相互影响。
3. **超大文件**：超大数据集（远超历轮规模）上传 → 导出 `.gfpkg`（含全量数据）→ 导入 → 跑链路，各段分别计时；考察超大文件读取/序列化压力。

报告含：环境、方法、每项的输入规模与耗时、结论与瓶颈点。

---

## 10. 文件结构（实现时落点）

- 新建：`backend/app/services/workflow_package.py`（导出收集+脱敏+打包、导入解压硬化+重连+重写+建链；纯服务层，三面共用）。
- 修改：`backend/app/routers/workflows.py`（加 export/import 两端点，复用服务层）。
- 修改：`backend/app/cli/commands/workflow.py`（`export`/`import` 取代 `dump`/`load`）。
- 修改：前端链路页（导出/导入按钮 + 报告弹窗）、`frontend/src/api/`（两个调用 + 报告类型）。
- 新建：`backend/tests/test_workflow_package.py`（§8.1）、活体脚本（§8.2，临时、用后删）。
