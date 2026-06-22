# GraphFlow 超大数据集上传、CRUD、Agent 查数与 LLM 跑数优化 Spec

**日期**：2026-06-22
**目标**：支持 1G-10G 级 CSV/Excel 数据集在 GraphFlow 中上传、加载、CRUD、Agent 定位查看和 LLM 节点批量跑数，并保证中断后可自动续跑、已完成结果不丢失。

**架构**：数据体统一从 SQLite 行存储迁移到文件分片存储，SQLite 只保存数据集元数据、manifest、版本关系和运行状态。所有公开行号使用文件可见行号；CSV/Excel 第 1 行是表头，第 2 行是第一条数据，JSONL 无表头且第 1 行是第一条记录。运行引擎从全量 `list[dict]` 传递改为落盘 artifact + 行级 checkpoint。

**技术栈**：FastAPI、SQLite/WAL、SQLAlchemy asyncio、Python 标准库 `csv/json/tempfile/pathlib/asyncio`、openpyxl read_only/write_only、现有 pandas 依赖。不新增 DuckDB、Polars、Arrow 等依赖。

---

## 1. 背景与目标

当前数据集上传路径会把文件整体读入内存，解析后生成完整 `list[dict]`，再把每行 JSON 写进 SQLite；下载和运行也存在全量查询、全量组装列表、单条 RunRow 保存大 JSON 的路径。这套实现适合小数据集，但 1G-10G、100K-10M 行数据会在内存、SQLite 文件膨胀、事务耗时、Excel 行数上限和运行恢复上同时成为瓶颈。

本 spec 的目标是定义后续 implementation plan 的上游约束：

- 上传和导入不因文件大小进入全量内存。
- 数据集读写、预览、Agent 查数都通过分片和行号定位完成。
- 行级和列级 CRUD 通过 copy-on-write 生成新版本，不原地修改上传源文件。
- LLM/http/QC/output 跑数按行持久化进度和输出，服务重启后自动续跑。
- 大数据下现有节点能力不需要显式切换模式；系统自动使用流式或磁盘 spill 策略。

---

## 2. 算法技术

### 2.1 分块上传落盘

上传文件必须先以固定 chunk 写入临时路径，禁止 `await upload.read()` 一次性读完整文件。推荐 chunk 大小为 1MB-8MB。写入完成后用原子 rename 变为正式上传文件，避免半文件被读到。

关键技术：

- chunked file copy
- temporary file + atomic rename
- upload progress counter
- import status persistence

### 2.2 CSV/Excel 流式解析

CSV 使用 Python 标准库 `csv.DictReader` 逐行解析。Excel 使用 openpyxl `load_workbook(..., read_only=True, data_only=True)` 和 `iter_rows(values_only=True)` 逐行解析。

解析约束：

- CSV/Excel 第 1 行作为表头，不作为业务数据行。
- Excel 多 sheet 保持当前语义：每个非空 sheet 可形成独立数据集，或由实现计划明确沿用现有多 sheet 命名规则。
- 空单元格统一表示为空字符串或 `None` 的策略必须在实现计划中固定，避免同一列不同入口语义漂移。
- JSONL 不含表头，每行是一个 JSON object；第 1 行就是第一条业务记录。

### 2.3 固定行数分片

数据体使用 canonical JSONL 分片保存。默认每 100,000 条业务数据行一个 shard。小数据和大数据都走同一套分片路径，避免双实现分叉。

每个 shard 只包含业务数据行，不包含 CSV/Excel 表头。表头保存在 manifest 的 columns/header 元数据里。

### 2.4 Manifest shard 索引

每个数据集保存 manifest，最小字段如下：

```json
{
  "original_format": "csv",
  "canonical_format": "jsonl",
  "version": 1,
  "version_of_dataset_id": null,
  "header_row": 1,
  "data_start_row": 2,
  "shard_size": 100000,
  "shards": [
    {
      "path": "datasets/12/v1/part-000001.jsonl",
      "start_data_idx": 0,
      "start_file_row": 2,
      "row_count": 100000,
      "columns": ["q", "answer"]
    }
  ]
}
```

JSONL 数据集无表头时，`header_row` 为 `null`，`data_start_row` 为 `1`，`start_file_row` 从 `1` 开始。

### 2.5 文件可见行号

API、Agent 工具、前端展示和错误信息统一使用文件可见行号。

- CSV/Excel 第 1 行是表头。
- CSV/Excel 第 2 行是第一条业务数据。
- JSONL 第 1 行是第一条业务数据。
- 内部可保留 0-based `data_idx`，但任何公开接口不得把它暴露成用户行号。

### 2.6 Range scan + column projection

读取指定行范围时，先通过 manifest 的 `start_file_row + row_count` 找到覆盖 shard，再只顺序读取命中的 shard 片段。Agent 和 API 的列筛选使用 column projection，只返回调用方选择的列。

推荐定位策略：

- shard 数少时线性扫描 manifest。
- shard 数多时按 `start_file_row` 做二分查找。
- 单 shard 内仍顺序读取，避免维护每行字节偏移导致 CRUD 版本化复杂化。

### 2.7 Copy-on-write 版本化 CRUD

所有 CRUD 都生成新数据集版本，不原地改上传源文件。

行级 v1：

- 按文件可见行号范围删除数据行。
- 按文件可见行号替换数据行。
- 在指定文件可见行号前插入数据行。

列级 v1：

- rename 列。
- drop 列。
- add 常量列。

CSV/Excel 表头行不能作为普通数据行删除或替换。表头变更必须通过列级操作完成。

### 2.8 StreamingResponse 下载

CSV/JSONL 下载使用 FastAPI `StreamingResponse`，逐行从 shard 读取并写出，不生成完整内存对象。CSV 使用 `csv.DictWriter` 写表头和行。

### 2.9 Excel write_only 导出与自动拆 sheet

Excel 导出使用 openpyxl `Workbook(write_only=True)`。当数据行数超过单 sheet 上限时，自动拆分多个 sheet。每个 sheet 都写入表头行，业务数据从该 sheet 第 2 行开始。

### 2.10 Artifact-backed pipeline

运行引擎中节点输入/输出不再用全量 `list[dict]` 串联。每个节点输出落盘为 artifact：

```json
{
  "run_id": 42,
  "node_id": "llm_1",
  "columns": ["q", "answer"],
  "row_count": 1000000,
  "shards": [
    {"path": "runs/42/llm_1/part-000001.jsonl", "start_file_row": 2, "row_count": 100000}
  ]
}
```

### 2.11 行级 checkpoint

LLM/http/QC 等逐行节点按行保存状态。SQLite 状态表至少要能表达：

- `run_id`
- `node_id`
- `file_row`
- `status`
- `attempt`
- `prompt_tokens`
- `completion_tokens`
- `error`
- `output_ref`

完成一行后先保证输出可读，再提交行状态。服务重启后扫描 `queued/running` run，跳过 `done` 行，继续未完成行。

### 2.12 asyncio worker pool

大量 LLM 跑数使用 `asyncio.Queue` 分发行任务，用用户级 semaphore 和节点级 concurrency 控制请求并发。worker 从输入 artifact 迭代行，遇到已完成状态则跳过，未完成则调用模型并持久化输出和状态。

### 2.13 SQLite spill

需要全量状态的节点不能把所有行载入内存。dedup、shuffle、sample 等操作使用 SQLite 临时表或磁盘中间态 spill。

约束：

- 临时表必须带 `run_id/node_id` 或使用独立临时数据库文件，避免并发 run 污染。
- 完成后清理临时表/临时库。
- 中断恢复时可重新构建 spill，不要求临时 spill 本身强恢复。

### 2.14 Reservoir sampling

sample 节点对大数据使用 reservoir sampling，在不知道总行数或不想全量载入时保持固定样本大小。

### 2.15 外部 shuffle

shuffle 节点为每行生成稳定随机 key，写入 SQLite 临时表或磁盘索引，再按 key 顺序流式输出。随机种子来自节点配置，确保同输入和同 seed 下结果可复现。

---

## 3. 数据语义

- CSV/Excel 表头是结构元数据，不作为 LLM 节点要处理的一条业务数据。
- JSONL 没有表头；列集合由记录 key 的有序 union 得到。
- 所有公开行号都是 1-based 文件可见行号。
- 数据集 `row_count` 表示业务数据行数，不包含 CSV/Excel 表头。
- 对外需要展示总文件行数时使用 `total_rows_including_header`。

---

## 4. API 与工具语义

### 4.1 上传

`POST /api/datasets/upload` 返回数据集 metadata，并通过事件推送导入进度。

新增或扩展字段：

- `status`
- `imported_rows`
- `original_format`
- `version`
- `version_of_dataset_id`
- `header_row`
- `data_start_row`
- `total_rows_including_header`

### 4.2 行读取

`GET /api/datasets/{id}/rows` 保留分页兼容，同时增加：

- `start_row`
- `end_row`
- `columns`

`start_row/end_row` 按文件可见行号解释，inclusive。CSV/Excel 请求第 1 行返回表头结构；请求第 2 行返回第一条业务数据。

### 4.3 CRUD 版本

新增 `POST /api/datasets/{id}/versions`，提交基础 CRUD 操作列表，返回新版本 dataset metadata。

### 4.4 导出

`GET /api/datasets/{id}/export?format=original|csv|jsonl|xlsx`，默认 `original`。`original` 按上传原格式导出；Excel 超过单 sheet 数据行上限时自动拆 sheet。

### 4.5 Agent 查数工具

新增 Agent 工具：

```python
async def read_dataset_rows(
    dataset_id: int,
    start_row: int,
    end_row: int,
    columns: list[str] | None = None,
) -> str:
    ...
```

返回 JSON 至少包含：

- `dataset_id`
- `total_rows_including_header`
- `header_row`
- `data_start_row`
- `start_row`
- `end_row`
- `columns`
- `rows`
- `truncated`

工具必须限制最大返回行数和 JSON 字符预算；超出时截断并返回 `truncated=true`。

---

## 5. 运行恢复语义

- 每行处理完成即持久化输出引用和行状态。
- 服务启动时继续调用现有 `resume_unfinished` 思路，恢复 `queued/running` run。
- 已完成行跳过，不重复调用 LLM。
- 崩溃瞬间已经写出但尚未提交状态的行允许重跑。
- 最终输出按 `file_row` 收敛，保留最新成功结果，避免重复行进入最终数据集。
- output 节点 `save_as_dataset` 直接把运行 artifact 注册成新数据集版本，不全量加载输出。

---

## 6. CRUD 语义

- 不原地改上传源文件。
- 每次行级或列级变更都生成新版本数据集。
- 新版本记录 `version_of_dataset_id` 和递增 `version`。
- 行级操作不能把 CSV/Excel 表头当普通数据行删除或替换。
- 列级 rename/drop/add 需要同步更新 manifest columns 和导出表头。
- CRUD 失败不得留下可见的半成品版本；临时分片必须在失败后清理或标记不可见。

---

## 7. 全量节点处理要求

现有节点能力不得通过 UI 显式切换“大数据模式”。实现应按输入规模自动选择流式或 spill 策略。

- map 类节点：LLM、http、rename、drop、cast、concat、add 常量列逐行流式处理。
- filter 类节点：逐行判断，命中行写入输出 artifact。
- dedup：SQLite spill，使用列组合 JSON hash 或规范化 key 做唯一约束。
- sample：reservoir sampling。
- shuffle：外部 shuffle，随机 key + SQLite 临时排序。
- 多分支合并：不得全量载入内存；需要按行号或共享 key 分段读取并校验。

---

## 8. 测试策略

### 8.1 Spec coverage

implementation plan 必须包含 spec coverage 检查：本 spec 中每个核心约束至少有一个测试或明确的验证命令覆盖。

### 8.2 上传与导入

- CSV 流式上传，包含 BOM、中文、空单元格、NUL 清理。
- Excel 流式上传，包含多 sheet、空 sheet、超过普通内存承载能力的模拟数据。
- manifest shard 字段正确。
- 导入失败时 dataset status 和错误信息正确，不返回 500。

### 8.3 行号语义

- CSV/Excel 第 1 行返回表头。
- CSV/Excel 第 2 行返回第一条业务数据。
- JSONL 第 1 行返回第一条记录。
- 跨 shard 范围读取无 off-by-one。

### 8.4 Agent 查数

- 指定行号范围读取。
- 指定列投影读取。
- 越界读取返回清晰错误或空范围。
- 返回预算触发截断。
- 租户隔离。

### 8.5 CRUD

- 删除数据行范围生成新版本。
- 替换数据行生成新版本。
- 插入数据行生成新版本。
- rename/drop/add 常量列生成新版本。
- 禁止删除或替换 CSV/Excel 表头行。
- 旧版本保持可读。

### 8.6 下载

- CSV/JSONL 走流式响应。
- Excel 使用 write_only。
- Excel 超过单 sheet 上限自动拆 sheet。
- 默认 `original` 格式按上传原格式导出。

### 8.7 运行恢复

- LLM 节点跑到一半中断后，服务重启自动续跑。
- 已完成行不重复调用 LLM。
- 崩溃窗口重复行最终按 `file_row` 去重。
- output 节点保存数据集不全量加载。

### 8.8 全量节点

- dedup 使用 SQLite spill。
- sample 使用 reservoir sampling。
- shuffle 使用外部 shuffle 且 seed 可复现。
- 大输入下 auto_process 不把全部行载入内存。

---

## 9. 后续 implementation plan 约束

后续实施计划必须引用本 spec，并按以下方式拆任务：

- 数据集存储任务引用“固定行数分片”“manifest shard 索引”“文件可见行号”。
- CRUD 任务引用“copy-on-write 版本化 CRUD”和“表头保护”。
- Agent 工具任务引用“range scan + column projection”。
- 下载任务引用“StreamingResponse”和“openpyxl write_only 自动拆 sheet”。
- 跑数引擎任务引用“artifact-backed pipeline”“行级 checkpoint”“asyncio worker pool”。
- 全量节点任务引用“SQLite spill”“reservoir sampling”“外部 shuffle”。
- 测试任务必须包含 spec coverage 检查。

如果实现中发现本 spec 某个约束不可行，先更新本 spec，再调整 implementation plan。
