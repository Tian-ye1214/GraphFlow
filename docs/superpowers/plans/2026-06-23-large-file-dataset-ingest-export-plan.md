# Spec 1 实现计划：数据集摄入/导出统一路径

> 执行方式：本会话内 TDD（test-first），小步频繁提交。测试在 `backend/tests/`（gitignore，用 `git add -f`）。
> 每个任务跑：`cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m pytest <file> -q`。

**全局约束**：单一路径不分大小文件；行数据只进分片不进 DB；尽量 stdlib；提交不含 "claude"/无 Co-Authored-By；不推 origin；不碰 .codegraph/.idea。

---

## Task 1：基础设施（config / 模型列 / 迁移 / pragma / 序列化小改）

**Files**：`backend/app/config.py`、`backend/app/models.py`、`backend/app/db.py`、`backend/app/services/dataset_store.py`、`backend/app/routers/datasets.py`

- config 加 `max_excel_upload_bytes: int = 200*1024*1024`、`ingest_concurrency: int = 2`。
- `Dataset` 加 `import_error: Mapped[str] = mapped_column(Text, default="")`。
- db.py：`busy_timeout` 5000→30000；`_migrate_sqlite_schema` datasets 块加 `import_error TEXT NOT NULL DEFAULT ''`。
- `_dumps_row`：两处 `json.dumps(...)` 加 `separators=(",",":")`。
- `_write_shards`：删循环内 `shards[-1]["columns"]=list(seen_columns)`，改 `open_shard` 不写 columns、分片关闭时（换片前 + finally）`shard["columns"]=list(seen_columns)` 定稿。
- `_out(ds)` 加 `"import_error": ds.import_error`。

- [ ] 测试：`_dumps_row({"a":1})` 输出 `{"a":1}`（无空格）；含 inf→null 仍成立；含 datetime 仍串化。
- [ ] 测试：迁移后 `Dataset.import_error` 可读写（建库后查列存在）。
- [ ] 实现 → 跑通 → 提交。

---

## Task 2：拆分「结构探测」与「批量写分片」

**Files**：`backend/app/services/dataset_store.py`、`backend/tests/test_large_file_ingest.py`(新)

把现有 `create_dataset_from_upload`/`_create_one_dataset` 拆成两段，供上传端点同步探测 + 后台批量：

```python
# 结构探测：只读表头/枚举 sheet，廉价。返回每个待建数据集的规格。
@dataclass
class ParseUnit:
    name: str
    columns: list[str]          # 已知表头(jsonl 可能为 [] 待补)
    header_row: int | None
    data_start_row: int
    original_format: str
    sheet_index: int | None     # Excel 用；csv/jsonl 为 None

def detect_upload_structure(filename: str, source_path: Path) -> list[ParseUnit]: ...

# 批量：按 ParseUnit 从源文件取行迭代器，写分片。纯同步、纯文件 IO，宜跑在 to_thread。
def rows_for_unit(unit: ParseUnit, source_path: Path) -> Iterable[dict]:
    # csv→_iter_csv_rows; jsonl→_iter_jsonl_rows; json→_iter_json_rows;
    # xlsx→重开 workbook 取 unit.sheet_index 那个 sheet 的 iter_rows
    ...

def parse_and_write_shards(*, source_path, unit, data_dir, user_id, dataset_id, version,
                           shard_size, progress_cb=None) -> tuple[dict, list[str], int]:
    # 调 _write_shards(rows_for_unit(unit, source_path), ...)，progress_cb 每分片回调
    ...
```

- `_write_shards` 增加可选 `progress_cb(row_count)`：每关闭一个分片调用一次。
- 现有 `create_dataset_from_upload` 暂保留（被旧测试/工具引用），内部改为「探测→建库→parse_and_write_shards」串起来（行为不变，确保旧测试绿）。

- [ ] 测试：`detect_upload_structure` 对 csv 返回 1 个 unit、列名正确；对多 sheet xlsx 返回 N 个 unit。
- [ ] 测试：`rows_for_unit` 对每格式产出正确行。
- [ ] 测试：`create_dataset_from_upload` 行为与改前一致（沿用现有 followups 用例回归）。
- [ ] 实现 → 跑通 → 提交。

---

## Task 3：IngestManager + resume + lifespan

**Files**：`backend/app/services/ingest_manager.py`(新)、`backend/app/main.py`、`backend/tests/test_large_file_ingest.py`

```python
class IngestManager:
    def __init__(self): self._running={}; self._sem=None
    def _semaphore(self): ...  # 懒建 asyncio.Semaphore(settings.ingest_concurrency)
    def submit(self, dataset_id, *, source_path, unit, delete_source_after, session_factory): ...
    async def _run_ingest(self, dataset_id, source_path, unit, delete_source_after, session_factory):
        async with self._semaphore():
            try:
                manifest, cols, n = await asyncio.to_thread(parse_and_write_shards, ..., progress_cb=mk_cb(dataset_id))
                # 短事务回填 ready + publish
            except Exception as e:
                # 短事务 failed + import_error + rmtree 孤儿分片 + publish
            finally:
                if delete_source_after and 该源无其它 importing 引用: source_path.unlink(missing_ok=True)
                self._running.pop(dataset_id, None)

async def resume_unfinished(session_factory) -> int:
    # 扫 status=="importing" → failed(import_error="服务重启，导入中断，请重传") + rmtree 分片 + 删源
```
- progress_cb 用 **stdlib sqlite3** 直连 `settings.data_dir/graphflow.db`：`UPDATE datasets SET imported_rows=? WHERE id=?`（每分片一次）。
- 模块级单例 `ingest_manager = IngestManager()`。
- main.py lifespan：`await ingest_resume_unfinished(get_session_factory())` 与 run resume 并列。

- [ ] 测试：submit 一个 csv unit → 轮询 status 由 importing→ready、row_count 正确、分片落盘、源文件删除。
- [ ] 测试：unit 指向损坏数据（注入抛错）→ status=failed、import_error 非空、孤儿分片清理。
- [ ] 测试：resume_unfinished 把残留 importing 标 failed + 清分片。
- [ ] 实现 → 跑通 → 提交。

---

## Task 4：重写 /upload 端点（同步探测 + 后台批量 + 门禁 + 回收）

**Files**：`backend/app/routers/datasets.py`、`backend/tests/test_large_dataset_followups.py`

- 落盘后：扩展名校验；`.xlsx/.xls` 源字节 > `max_excel_upload_bytes` → 422；`shutil.disk_usage` 预检 → 422。
- `detect_upload_structure` → 为每 unit 建 `Dataset(status="importing", columns=unit.columns)`，短事务 commit，`publish`。
- 对每 unit `ingest_manager.submit(...)`（最后一个 unit 带 `delete_source_after=True` 或按"该源全部完成"判断）。
- 返回占位 Dataset 列表（status=importing）。
- 解析失败不再走端点 422（除上述同步校验），改后台 failed。

- [ ] 测试：上传 csv → 立即 200 + status=importing；轮询变 ready；源文件最终删除。
- [ ] 测试：上传损坏 csv（中途畸形）→ 200 importing → 轮询变 failed + import_error。
- [ ] 测试：上传 >门禁的伪 xlsx（构造大字节）→ 422。
- [ ] 测试：现有 followups 里依赖"上传同步返回 ready"的用例改为轮询（或加 helper `await _upload_ready(...)`）。
- [ ] 实现 → 跑通 → 提交。

---

## Task 5：CSV 流式导出 + xlsx to_thread

**Files**：`backend/app/services/dataset_store.py`、`backend/app/routers/datasets.py`、`backend/tests/test_large_dataset_followups.py`

- 新 `iter_csv_lines(session, ds, data_dir)` async 生成器：`csv.writer` 写 `io.StringIO`，先 yield 表头行，再逐行 yield（用 `_jsonify_nested` 串化嵌套，保持与现状一致）。
- export 端 csv 分支 → `StreamingResponse(iter_csv_lines(...), media_type="text/csv; charset=utf-8", headers=_attachment_headers)`，删该分支 exports/ 落盘 + BackgroundTask。
- xlsx 分支：`path = await asyncio.to_thread(write_xlsx_export, ...)`（注意 write_xlsx_export 现为 async，需拆出同步核心给 to_thread；或保持 async 但内部 wb.save 前的循环本就同步——把整函数同步核心 `_write_xlsx_sync` 抽出，async 包装 `await asyncio.to_thread(_write_xlsx_sync, ...)`）。

- [ ] 测试：csv 导出内容正确（含嵌套 JSON 往返）、exports/ 零残留（流式无落盘）。
- [ ] 测试：xlsx 导出仍 200、内容正确（回归现有 Gap 用例）。
- [ ] 实现 → 跑通 → 提交。

---

## Task 6：gf CLI 流式下载

**Files**：`backend/app/cli/client.py`、必要时 `cli/commands/{dataset,run,workflow}.py`、`backend/tests/`

- client download helper 改 `http.stream`+`iter_bytes(1<<20)` 写盘；read 超时放开。
- 各 caller 用统一 helper。

- [ ] 测试：下载大响应（构造 >内存友好大小的伪响应或 monkeypatch）写盘正确、不一次性读入。
- [ ] 实现 → 跑通 → 提交。

---

## Task 7：活体脚本扩展 + 全量回归

**Files**：`backend/tools/large_dataset_live.py`

- 加：上传→轮询 importing→ready；失败态；Excel 门禁 422；CSV 流式导出 + exports 零残留；源文件回收；CLI 流式下载。
- 全量后端套件绿；（重启后）线上活体全过。

- [ ] 跑全量 pytest 绿 → 提交 → 合并 master → 删分支 → 更新记忆。
```
