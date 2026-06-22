# Spec 1：数据集摄入/导出统一路径（面向 1–10G）设计

**日期**：2026-06-23
**范围**：数据集 `/upload`、`/export`、`/rows` 与 gf CLI 下载。**不含**跑数引擎/Agent（那是 Spec 2）。

## 目标

让文件上传→存储→导出→下载这条链路在 1–10G CSV/Excel 下：
1. 上传期间**不阻塞事件循环**、不被代理/客户端超时；
2. **不压垮 SQLite**（行数据只进磁盘分片，DB 只存 KB 级元信息——现状已具备，需保持并补短事务纪律）；
3. **导出/下载不把整文件读进内存**；
4. 尽量用 **stdlib**（asyncio/sqlite3/csv/io/shutil/zipfile）。

## 最高约束：单一路径，绝不分大/小文件

所有尺寸走**同一条**上传/导出/下载代码路径。小文件只是瞬间完成。体积阈值只能作为**护栏式拒绝（422）**，绝不能产生"大文件专用模式"的功能分叉。

## 架构

### 上传（同步部分，秒级返回）
`POST /api/datasets/upload` 改为：
1. 流式落盘到 `uploads/<uid>/.<id>_<name>.tmp` → replace（现状保留）。
2. **廉价同步校验**（失败 → 422 + 清理）：
   - 扩展名受支持（csv/jsonl/json/xlsx/xls，其中 .xls/.xlsb 仍按现状会失败，留 Spec 后续；本期不在受理白名单做改动）；
   - **Excel 体积门禁**：源 .xlsx/.xls 字节 > `settings.max_excel_upload_bytes`（默认 200MB）→ 422「Excel 过大，请导出 CSV 上传」；
   - **磁盘空间预检**：`shutil.disk_usage(data_dir)` 剩余 < 源文件大小 × 系数（如 3）→ 422。
3. **结构探测（仅表头/分 sheet，廉价）**：
   - CSV/JSONL/JSON：读首行/首记录得列名 → 1 个 Dataset；
   - Excel（已被体积门禁约束在 ≤200MB）：`load_workbook(read_only=True)` 枚举 sheet + 每 sheet 表头 → N 个 Dataset。
   - 据此创建 `Dataset(status="importing", columns=已知列, row_count=0)` 占位行，**短事务 commit**（立即可见），`publish` dataset 事件。
4. 把"**逐行写分片**"这步提交给 `IngestManager` 后台执行，端点立即返回占位 Dataset 列表（status=importing）。

> 结构探测同步、批量行→分片后台：既让响应立刻带回真实 dataset id/列名/分 sheet，又把唯一的重活（千万行 json.dumps 写盘）放后台。Excel 重开工作簿在后台线程进行（体积已受门禁约束）。

### 后台摄入：`app/services/ingest_manager.py`（镜像 `RunManager`）
- `IngestManager.submit(dataset_id, source_path, parse_spec, user_id)` → `asyncio.create_task(_run_ingest(...))`；`_running: dict[int, Task]`，完成即 forget。
- 全局并发上限 `asyncio.Semaphore(settings.ingest_concurrency)`（默认 2），超额排队（占位行保持 importing）。
- `_run_ingest`：
  - `manifest, columns, row_count = await asyncio.to_thread(_parse_and_write_shards, parse_spec, ...)`（解析+写分片全程在线程，事件循环不被占）；
  - **进度**：`_parse_and_write_shards` 内每写完一个分片用 **stdlib `sqlite3`** 直连 `UPDATE datasets SET imported_rows=? WHERE id=?`（短写、WAL 安全、不跨线程碰 async session）；
  - 成功：短事务回填 `manifest_json/columns_json/row_count/imported_rows/total_rows_including_header/status="ready"`、`publish`；该 upload 的**全部** dataset 成功后删除源文件；
  - 失败：短事务 `status="failed"` + `import_error=文案`、`publish`、`rmtree` 该 dataset 的孤儿分片目录。
- `resume_unfinished(session_factory)`：进程启动时扫 `status=="importing"` 的 Dataset → 标 `failed`（import_error="服务重启，导入中断，请重传") + 清孤儿分片 + 删源文件。**不自动重解析**（KISS）。在 `main.py` lifespan 调用（与 run 的 resume 并列）。

### 失败语义变化（已与用户确认）
深层解析错（CSV 中途畸形、xlsx sheet 截断、非有限数等）从同步 422 变为 `status="failed"` + `import_error`。仅"上传时即可判定"的错（扩展名/体积门禁/磁盘/空文件）仍同步 422。

### 数据模型
- `Dataset` 新增 `import_error: Mapped[str] = mapped_column(Text, default="")`；
- db.py `_migrate_sqlite_schema` 的 datasets 块加 `import_error TEXT NOT NULL DEFAULT ''`（沿用现有 ADD COLUMN 范式）；
- `_out(ds)` 输出加 `import_error`。

### 短事务纪律
- 占位创建（commit）/ 进度（独立 sqlite3 短写）/ 完成回填（commit）三段都是毫秒级，解析期不持写事务；
- `db.py` `busy_timeout` 5000 → 30000。

### 导出（统一流式）
- **CSV**：改 `StreamingResponse`，`iter_csv_lines` 生成器用 `csv.writer` 写 `io.StringIO`、逐批 yield，删 `exports/` 落盘与该路径 BackgroundTask（与 jsonl 同构）。
- **xlsx**：zip 格式难真流式，保留落盘 + BackgroundTask 回收，但 `write_xlsx_export` 用 `await asyncio.to_thread(...)` 卸载，避免阻塞事件循环；超大数据集导出 xlsx 引导用 csv（行数阈值提示，护栏不分叉）。
- **jsonl**：已流式，不动。

### gf CLI 下载（流式落盘）
`cli/client.py` 的 download 改：
```python
with self.http.stream("GET", path, params=params) as r:
    r.raise_for_status()
    with out.open("wb") as f:
        for chunk in r.iter_bytes(1 << 20):
            f.write(chunk)
```
读超时 `httpx.Timeout(connect=30, read=None)`。覆盖 dataset/run/workflow 下载各 caller。

### 顺手小改（在已触及的文件内）
- `_dumps_row` 加 `separators=(",", ":")`（紧凑、省 ~7% 盘）；
- 删 `_write_shards` 循环内逐行 `shards[-1]["columns"]=list(seen_columns)`，改分片关闭时一次写定稿。

## 配置新增（config.py）
- `max_excel_upload_bytes: int = 200 * 1024 * 1024`
- `ingest_concurrency: int = 2`

## 测试
- 单测 `backend/tests/`（`git add -f`）：上传返回 importing→轮询 ready；解析失败→failed+import_error；Excel 体积门禁→422；磁盘预检→422；CSV 流式导出内容正确且 exports/ 零残留；源文件解析后被删；resume_unfinished 把残留 importing 标 failed + 清孤儿；`_dumps_row` 紧凑分隔符。
- 活体 `tools/large_dataset_live.py` 扩：上传→SSE/轮询→ready、失败态、门禁、流式导出、源文件回收、CLI 流式下载。
- 全量后端套件须绿。

## 显式不做（留 Spec 2 / 后续）
跑数引擎流式化、RunRow.data_json 瘦身、Agent 预览修复（Spec 2）；Excel SAX 真流式、分片 gzip、导出 Range 断点续传、按字节切分片、断点续传重解析、Parquet 列式。

## 兼容/风险
- 失败语义改变需前端配合（status 轮询/SSE 已有 publish 链路）；
- 结构探测对 Excel 仍开一次工作簿（受 200MB 门禁约束，不会 OOM）；
- 源文件删除后无法"原样下载原始字节"（用户已确认接受，导出从分片重生成）；
- to_thread 用默认线程池 + ingest 信号量限并发，避免饿死导出/agent 的 to_thread。
