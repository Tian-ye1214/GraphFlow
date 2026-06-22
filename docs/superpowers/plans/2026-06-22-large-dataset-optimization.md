# Large Dataset Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the design in `docs/superpowers/specs/2026-06-22-large-dataset-optimization-design.md`: file-sharded datasets, visible file-row addressing, versioned CRUD, Agent row-range inspection, and resumable high-volume LLM runs.

**Architecture:** Datasets move from primary SQLite row storage to canonical JSONL shard files with a manifest stored on `Dataset`. SQLite remains the metadata, ownership, version, and run-status store. The runner moves incrementally from whole-list node handoff to artifact-backed node outputs with row-level checkpoints; operations that need whole-dataset state use SQLite spill instead of memory.

**Tech Stack:** FastAPI, SQLAlchemy asyncio, SQLite/WAL, Python standard library `csv/json/tempfile/pathlib/asyncio`, openpyxl read_only/write_only, existing pandas only where tests or legacy helpers already require it.

---

## Global Constraints

- Spec source of truth: `docs/superpowers/specs/2026-06-22-large-dataset-optimization-design.md`.
- Do not add DuckDB, Polars, Arrow, or new storage dependencies.
- Public row numbers are file-visible, 1-based row numbers. CSV/Excel row 1 is the header; JSONL row 1 is the first record.
- `Dataset.row_count` means business data rows and does not include CSV/Excel headers.
- Keep existing small-dataset behavior compatible from the API caller perspective.
- Preserve tenant isolation on every dataset, run, and Agent tool read path.
- Do not stage `.codegraph/`, `.idea/`, `__pycache__/`, `.pyc`, or generated export files.
- Tests under `backend/tests/` are ignored by git in this repo; use `git add -f backend/tests/...` when committing test changes.
- Use focused commits after each task. Commit messages should be Chinese, should not include "claude", and should not add `Co-Authored-By`.

## File Structure

- Create `backend/app/services/dataset_store.py`: manifest model helpers, upload-to-shards, lazy legacy migration, range reads, column projection, streaming CSV/JSONL export, write_only Excel export.
- Create `backend/app/services/dataset_crud.py`: copy-on-write row and column operations that produce a new dataset version.
- Create `backend/app/services/run_artifacts.py`: node output artifact manifests, append-only JSONL shard writers, row-result lookup by file row.
- Modify `backend/app/models.py`: add dataset manifest/version/status fields and run-row artifact/checkpoint fields.
- Modify `backend/app/db.py`: SQLite schema migration for new fields and pragmatic indexes.
- Modify `backend/app/routers/datasets.py`: upload, rows, export, versions endpoints switch to dataset store.
- Modify `backend/app/agent/data_preview.py`: add `read_dataset_rows` and route preview/describe through dataset store.
- Modify `backend/app/agent/node_info.py`: read latest run outputs through artifacts where available.
- Modify `backend/app/engine/runner.py`: phased artifact-backed input/output/LLM/http/QC execution and checkpoint resume.
- Modify `backend/app/engine/nodes.py`: add stream-capable operation helpers and SQLite-spill helpers for dedup/sample/shuffle.
- Modify `backend/app/services/workflow_package.py`: export/import datasets via shard store instead of direct `DatasetRow` scans.
- Modify `frontend/src/pages/DatasetsPage.tsx` and `frontend/src/api/types.ts`: show new metadata, support visible-row range preview, download original format, and basic versioned CRUD entry points.

---

## Task 1: Schema, Metadata, and Spec Coverage Gate

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/db.py`
- Test: `backend/tests/test_large_dataset_schema.py`
- Reference: `docs/superpowers/specs/2026-06-22-large-dataset-optimization-design.md`

- [ ] **Step 1: Write schema tests**

Create `backend/tests/test_large_dataset_schema.py`:

```python
import json

from sqlalchemy import select

from app.models import Dataset, RunRow, User


async def test_dataset_large_storage_metadata_defaults(session_factory):
    async with session_factory() as s:
        u = User(username="meta_user", display_name="x")
        s.add(u)
        await s.flush()
        ds = Dataset(user_id=u.id, name="big", columns_json="[]")
        s.add(ds)
        await s.commit()
        ds_id = ds.id

    async with session_factory() as s:
        ds = await s.get(Dataset, ds_id)
        assert ds.status == "ready"
        assert ds.imported_rows == 0
        assert ds.original_format == ""
        assert ds.version == 1
        assert ds.version_of_dataset_id is None
        assert ds.header_row is None
        assert ds.data_start_row == 1
        assert ds.total_rows_including_header == 0
        assert json.loads(ds.manifest_json) == {}


async def test_runrow_checkpoint_fields_defaults(session_factory):
    async with session_factory() as s:
        rr = RunRow(run_id=1, node_id="n", row_idx=0)
        s.add(rr)
        await s.commit()
        row_id = rr.id

    async with session_factory() as s:
        rr = await s.get(RunRow, row_id)
        assert rr.file_row is None
        assert rr.output_ref == ""
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_large_dataset_schema.py`

Expected: FAIL because `Dataset` and `RunRow` do not have the new fields.

- [ ] **Step 3: Add model fields**

Modify `backend/app/models.py`:

```python
class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    source: Mapped[str] = mapped_column(default="upload")
    original_filename: Mapped[str] = mapped_column(default="")
    file_path: Mapped[str] = mapped_column(default="")
    row_count: Mapped[int] = mapped_column(default=0)
    columns_json: Mapped[str] = mapped_column(Text, default="[]")
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(default="ready")
    imported_rows: Mapped[int] = mapped_column(default=0)
    original_format: Mapped[str] = mapped_column(default="")
    version: Mapped[int] = mapped_column(default=1)
    version_of_dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), default=None)
    header_row: Mapped[int | None] = mapped_column(default=None)
    data_start_row: Mapped[int] = mapped_column(default=1)
    total_rows_including_header: Mapped[int] = mapped_column(default=0)
    run_id: Mapped[int | None] = mapped_column(default=None)
    node_id: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

Modify `RunRow` with:

```python
    file_row: Mapped[int | None] = mapped_column(default=None)
    output_ref: Mapped[str] = mapped_column(Text, default="")
```

Add indexes:

```python
Index("ix_datasets_version_parent", "version_of_dataset_id", "version")
Index("ix_run_row_file_row", "run_id", "node_id", "file_row", unique=True)
```

Keep the existing `ix_run_row_unit` index for compatibility during the runner migration.

- [ ] **Step 4: Add SQLite migrations**

Modify `_migrate_sqlite_schema()` in `backend/app/db.py` to add missing columns with defaults:

```python
dataset_adds = {
    "manifest_json": "TEXT NOT NULL DEFAULT '{}'",
    "status": "VARCHAR NOT NULL DEFAULT 'ready'",
    "imported_rows": "INTEGER NOT NULL DEFAULT 0",
    "original_format": "VARCHAR NOT NULL DEFAULT ''",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "version_of_dataset_id": "INTEGER",
    "header_row": "INTEGER",
    "data_start_row": "INTEGER NOT NULL DEFAULT 1",
    "total_rows_including_header": "INTEGER NOT NULL DEFAULT 0",
}
for name, ddl in dataset_adds.items():
    if rows and name not in cols:
        await conn.exec_driver_sql(f"ALTER TABLE datasets ADD COLUMN {name} {ddl}")
```

Then inspect `run_rows` and add:

```python
run_row_adds = {
    "file_row": "INTEGER",
    "output_ref": "TEXT NOT NULL DEFAULT ''",
}
```

Create missing indexes with `CREATE INDEX IF NOT EXISTS`.

- [ ] **Step 5: Run schema tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_large_dataset_schema.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/models.py backend/app/db.py
git add -f backend/tests/test_large_dataset_schema.py
git commit -m "feat(dataset): 增加分片存储元数据字段"
```

---

## Task 2: Dataset Store Service and Lazy Legacy Migration

**Files:**
- Create: `backend/app/services/dataset_store.py`
- Test: `backend/tests/test_dataset_store.py`
- Reference: spec sections 2.1-2.6, 2.8-2.9, 3

- [ ] **Step 1: Write service tests**

Create `backend/tests/test_dataset_store.py` with tests for:

```python
async def test_import_csv_to_shards_visible_rows(tmp_path, session_factory):
    # CSV header is file row 1; first data row is file row 2.
    # Use shard_size=2 so range reads cross shards.


async def test_import_jsonl_has_no_header(tmp_path, session_factory):
    # JSONL first file row is first data row.


async def test_read_range_projects_columns_and_truncates(tmp_path, session_factory):
    # read_dataset_range returns selected columns only and preserves start/end file rows.


async def test_lazy_migrates_legacy_dataset_rows(tmp_path, session_factory):
    # Dataset with DatasetRow rows and empty manifest_json is converted to shards on first read.


async def test_stream_csv_and_jsonl_exports_from_shards(tmp_path, session_factory):
    # Export does not require DatasetRow and preserves columns/header.


async def test_export_xlsx_splits_sheets(monkeypatch, tmp_path, session_factory):
    # Monkeypatch EXCEL_MAX_DATA_ROWS_PER_SHEET to 2 and assert two sheets are created.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_dataset_store.py`

Expected: FAIL because `app.services.dataset_store` does not exist.

- [ ] **Step 3: Implement manifest helpers**

Create `backend/app/services/dataset_store.py` with constants and helpers:

```python
SHARD_SIZE = 100_000
MAX_AGENT_ROWS = 500
MAX_AGENT_CHARS = 60_000
EXCEL_MAX_DATA_ROWS_PER_SHEET = 1_048_575

def dataset_root(data_dir: Path, user_id: int, dataset_id: int, version: int) -> Path:
    return data_dir / "datasets" / str(user_id) / str(dataset_id) / f"v{version}"

def load_manifest(ds: Dataset) -> dict:
    return json.loads(ds.manifest_json or "{}")

def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))

def visible_total(row_count: int, header_row: int | None) -> int:
    return row_count + (1 if header_row is not None else 0)
```

- [ ] **Step 4: Implement streaming imports**

Implement:

```python
async def create_dataset_from_upload(
    session: AsyncSession,
    *,
    user_id: int,
    filename: str,
    source_path: Path,
    data_dir: Path,
    shard_size: int = SHARD_SIZE,
) -> list[Dataset]:
    ...
```

Rules:

- CSV creates one dataset.
- JSONL creates one dataset.
- XLSX creates one dataset per non-empty sheet using existing naming semantics.
- The function writes shards before committing dataset status `ready`.
- On parser error, dataset is not made visible unless a dataset row already exists with `status='failed'`; use a transaction boundary that keeps callers from seeing partial ready data.

- [ ] **Step 5: Implement range reads**

Implement:

```python
async def ensure_dataset_materialized(session: AsyncSession, ds: Dataset, data_dir: Path) -> Dataset:
    ...

async def read_dataset_range(
    session: AsyncSession,
    ds: Dataset,
    *,
    data_dir: Path,
    start_row: int,
    end_row: int,
    columns: list[str] | None = None,
    max_rows: int | None = None,
    max_chars: int | None = None,
) -> dict:
    ...
```

Return shape:

```python
{
    "dataset_id": ds.id,
    "total_rows": ds.row_count,
    "total_rows_including_header": ds.total_rows_including_header,
    "header_row": ds.header_row,
    "data_start_row": ds.data_start_row,
    "start_row": start_row,
    "end_row": end_row,
    "columns": selected_columns,
    "rows": rows,
    "truncated": truncated,
}
```

For CSV/Excel row 1, return a synthetic header row like `{"__row_type": "header", "columns": selected_columns}`. Business rows remain plain dicts.

- [ ] **Step 6: Implement exports**

Implement:

```python
async def iter_jsonl_lines(session: AsyncSession, ds: Dataset, data_dir: Path):
    ...

async def write_csv_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ...

async def write_jsonl_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ...

async def write_xlsx_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ...
```

Use `csv.DictWriter` and openpyxl `Workbook(write_only=True)`.

- [ ] **Step 7: Implement lazy legacy migration**

When `manifest_json == "{}"` and legacy `DatasetRow` records exist:

- Create shard files from `DatasetRow` ordered by `idx`.
- Set `original_format` from `original_filename` suffix, falling back to `jsonl`.
- For legacy CSV/XLSX imports, set `header_row=1` and `data_start_row=2`; for legacy JSONL/JSON, set `header_row=None` and `data_start_row=1`.
- Set `manifest_json`, `imported_rows`, and `total_rows_including_header`.
- Leave `DatasetRow` rows in place for rollback compatibility until a later cleanup task.

- [ ] **Step 8: Run store tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_dataset_store.py`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/services/dataset_store.py
git add -f backend/tests/test_dataset_store.py
git commit -m "feat(dataset): 增加分片存储服务"
```

---

## Task 3: Dataset API Upload, Rows, and Export

**Files:**
- Modify: `backend/app/routers/datasets.py`
- Modify: `backend/app/services/export.py`
- Test: `backend/tests/test_datasets.py`
- Test: `backend/tests/test_large_dataset_api.py`
- Reference: spec sections 4.1, 4.2, 4.4

- [ ] **Step 1: Write API tests**

Create `backend/tests/test_large_dataset_api.py`:

```python
async def test_upload_csv_returns_large_metadata(auth_client, tmp_path):
    # assert status, imported_rows, original_format, version, header_row, data_start_row


async def test_rows_start_end_include_csv_header(auth_client):
    # /rows?start_row=1&end_row=2 returns header marker and first data row


async def test_rows_jsonl_first_row_is_record(auth_client):
    # JSONL start_row=1 returns first record, no header marker


async def test_rows_column_projection(auth_client):
    # columns=q,answer only returns those keys


async def test_export_original_csv_streams(auth_client):
    # default/original returns CSV for CSV upload


async def test_export_original_xlsx(auth_client):
    # xlsx upload exports xlsx content-type/filename and readable workbook
```

- [ ] **Step 2: Run API tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_large_dataset_api.py`

Expected: FAIL because routes do not expose the new behavior.

- [ ] **Step 3: Change upload route to chunk to temp file**

In `upload()`:

- Create `uploads/{user_id}`.
- Write each `UploadFile` to `*.tmp` in chunks using `await f.read(1024 * 1024)`.
- Rename temp file to the safe final upload path.
- Call `create_dataset_from_upload(...)`.
- Return `_out(ds)` for every dataset produced by the store.

Do not keep `content = await f.read()`.

- [ ] **Step 4: Expand `_out(ds)` metadata**

Add fields:

```python
"status": ds.status,
"imported_rows": ds.imported_rows,
"original_format": ds.original_format,
"version": ds.version,
"version_of_dataset_id": ds.version_of_dataset_id,
"header_row": ds.header_row,
"data_start_row": ds.data_start_row,
"total_rows_including_header": ds.total_rows_including_header,
```

- [ ] **Step 5: Change rows endpoint**

Keep pagination compatibility:

- If `start_row/end_row` are absent, convert `page/page_size` to public row bounds for business rows.
- If `start_row/end_row` are present, call `read_dataset_range`.
- Parse `columns` as comma-separated list.

For old callers, return `{"total": ds.row_count, "rows": [...]}`. For range callers, include the expanded range metadata.

- [ ] **Step 6: Change export endpoint**

Use `format: Literal["original", "jsonl", "csv", "xlsx"] = "original"`.

Rules:

- `original` resolves to `ds.original_format` if it is one of `csv/jsonl/xlsx`; JSON uploads export as JSONL.
- `jsonl` can use `StreamingResponse(iter_jsonl_lines(...))`.
- `csv/xlsx` write to a temp export path using dataset_store export helpers, then return `FileResponse`.

- [ ] **Step 7: Keep legacy tests green**

Update current `backend/tests/test_datasets.py` only where assertions need new metadata or default export format. Do not weaken existing safety tests for overlong filenames, user isolation, or bad content.

- [ ] **Step 8: Run dataset tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_datasets.py tests/test_large_dataset_api.py tests/test_dataset_store.py`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/routers/datasets.py backend/app/services/export.py
git add -f backend/tests/test_datasets.py backend/tests/test_large_dataset_api.py
git commit -m "feat(dataset): 上传读取导出改为分片存储"
```

---

## Task 4: Versioned Dataset CRUD

**Files:**
- Create: `backend/app/services/dataset_crud.py`
- Modify: `backend/app/routers/datasets.py`
- Test: `backend/tests/test_dataset_crud_versions.py`
- Reference: spec sections 2.7, 6

- [ ] **Step 1: Write CRUD tests**

Create tests:

```python
async def test_delete_visible_rows_creates_new_version(auth_client):
    # CSV rows 2-3 are deleted, old dataset unchanged, new version row_count decreased.


async def test_replace_visible_row_creates_new_version(auth_client):
    # Replaces file row 2 only.


async def test_insert_before_visible_row_creates_new_version(auth_client):
    # Inserts before file row 2.


async def test_column_rename_drop_add_constant(auth_client):
    # Applies rename/drop/add and updates columns.


async def test_cannot_delete_or_replace_header_row(auth_client):
    # CSV row 1 delete/replace returns 422.


async def test_crud_rejects_foreign_dataset(auth_client, session_factory):
    # Tenant isolation returns 404.
```

- [ ] **Step 2: Run CRUD tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_dataset_crud_versions.py`

Expected: FAIL because `/api/datasets/{id}/versions` does not exist.

- [ ] **Step 3: Implement CRUD service**

Create `apply_dataset_operations(...)`:

```python
async def apply_dataset_operations(
    session: AsyncSession,
    *,
    source: Dataset,
    user_id: int,
    data_dir: Path,
    operations: list[dict],
) -> Dataset:
    ...
```

Operation shapes:

```json
{"op":"delete_rows","start_row":2,"end_row":10}
{"op":"replace_rows","start_row":2,"rows":[{"q":"new"}]}
{"op":"insert_rows","before_row":2,"rows":[{"q":"new"}]}
{"op":"rename_column","from":"old","to":"new"}
{"op":"drop_column","name":"secret"}
{"op":"add_constant_column","name":"source","value":"manual"}
```

Implement by streaming source rows and writing a new shard set. Apply column transforms before row writes. Validate header-row operations before writing any output.

- [ ] **Step 4: Add versions endpoint**

Add to `datasets.py`:

```python
@router.post("/{ds_id}/versions")
async def create_dataset_version(ds_id: int, body: dict, ...):
    ds = await _get_owned(ds_id, user, session)
    ops = body.get("operations")
    if not isinstance(ops, list):
        raise HTTPException(status_code=422, detail="operations must be a list")
    new_ds = await apply_dataset_operations(...)
    publish(user.id, "dataset", new_ds.id)
    return _out(new_ds)
```

- [ ] **Step 5: Run CRUD tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_dataset_crud_versions.py tests/test_large_dataset_api.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/services/dataset_crud.py backend/app/routers/datasets.py
git add -f backend/tests/test_dataset_crud_versions.py
git commit -m "feat(dataset): 增加版本化行列 CRUD"
```

---

## Task 5: Agent Dataset Row Tool

**Files:**
- Modify: `backend/app/agent/data_preview.py`
- Modify: `backend/app/agent/tools.py`
- Modify: `backend/app/routers/agent.py`
- Test: `backend/tests/test_agent_dataset_tools.py`
- Reference: spec sections 2.6, 4.5

- [ ] **Step 1: Write Agent tool tests**

Create tests for:

```python
async def test_agent_read_dataset_rows_visible_range(session_factory, tmp_path):
    # Tool returns CSV header at row 1 and data at row 2.


async def test_agent_read_dataset_rows_column_projection(session_factory, tmp_path):
    # Tool returns selected columns only.


async def test_agent_read_dataset_rows_truncates_budget(session_factory, tmp_path):
    # Tool marks truncated true.


async def test_agent_read_dataset_rows_rejects_foreign_dataset(session_factory, tmp_path):
    # Foreign dataset returns dataset_not_found-style JSON, not raw data.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_agent_dataset_tools.py`

Expected: FAIL because the tool does not exist.

- [ ] **Step 3: Add tool method**

In `WorkflowDataPreview`, add:

```python
async def read_dataset_rows(
    self,
    dataset_id: int,
    start_row: int,
    end_row: int,
    columns: list[str] | None = None,
) -> str:
    ...
```

Use `_fit_budget` and dataset_store `read_dataset_range`.

- [ ] **Step 4: Register tool**

Update `make_preview_tools(...)` so general Agent contexts include `previewer.read_dataset_rows`. For node-assist contexts, include both current workflow preview tools and `read_dataset_rows`.

- [ ] **Step 5: Make preview/describe use dataset_store**

Replace direct `DatasetRow` reads in `_dataset_preview()` and `_dataset_total_rows()` with dataset_store range reads and metadata. Keep `limit` caps unchanged.

- [ ] **Step 6: Run Agent tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_agent_dataset_tools.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/agent/data_preview.py backend/app/agent/tools.py backend/app/routers/agent.py
git add -f backend/tests/test_agent_dataset_tools.py
git commit -m "feat(agent): 增加数据集指定行范围查看工具"
```

---

## Task 6: Frontend Dataset Controls

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/pages/DatasetsPage.tsx`
- Test: `frontend` typecheck/build command from project conventions
- Reference: spec sections 4.1-4.4

- [ ] **Step 1: Update API types**

Add fields to `Dataset`:

```ts
status: string
imported_rows: number
original_format: string
version: number
version_of_dataset_id: number | null
header_row: number | null
data_start_row: number
total_rows_including_header: number
```

Extend `RowsPage` with optional range metadata:

```ts
total_rows_including_header?: number
header_row?: number | null
data_start_row?: number
start_row?: number
end_row?: number
truncated?: boolean
```

- [ ] **Step 2: Add range preview controls**

In `DatasetsPage.tsx`, add small numeric controls in the preview drawer:

- `startRow`
- `endRow`
- comma-separated `selectedColumns`

Call:

```ts
api.get<RowsPage>(`/api/datasets/${preview.id}/rows?start_row=${startRow}&end_row=${endRow}&columns=${encodeURIComponent(selectedColumns)}`)
```

Keep existing pagination for default preview when range controls are empty.

- [ ] **Step 3: Add download original action**

Add a "下载" action that calls `/api/datasets/{id}/export?format=original` and saves the response using the existing download helper pattern in `frontend/src/api/client.ts`.

- [ ] **Step 4: Add basic CRUD version action**

Expose a simple modal for v1 operations:

- delete row range
- rename column
- drop column
- add constant column

Submit to `POST /api/datasets/{id}/versions`. After success, reload list and show the new dataset version.

- [ ] **Step 5: Run frontend verification**

Run from repo root or frontend folder according to current project scripts:

`cd "C:/Users/Admin/Desktop/GraphFlow/frontend" && npm run build`

Expected: build succeeds.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add frontend/src/api/types.ts frontend/src/pages/DatasetsPage.tsx
git commit -m "feat(ui): 增加大数据集行范围预览和版本化操作入口"
```

---

## Task 7: Run Artifacts and Output Dataset Registration

**Files:**
- Create: `backend/app/services/run_artifacts.py`
- Modify: `backend/app/engine/runner.py`
- Modify: `backend/app/routers/runs.py`
- Test: `backend/tests/test_run_artifacts.py`
- Reference: spec sections 2.10, 2.11, 5

- [ ] **Step 1: Write artifact tests**

Create tests:

```python
async def test_artifact_writer_appends_shards_and_manifest(tmp_path):
    # Writer splits after small shard_size and returns output_ref values.


async def test_output_node_registers_artifact_as_dataset(session_factory, tmp_path):
    # save_as_dataset creates Dataset manifest without loading all rows.


async def test_run_rows_endpoint_reads_artifact_output(session_factory, tmp_path):
    # /runs/{id}/rows returns artifact-backed output rows.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_run_artifacts.py`

Expected: FAIL because `run_artifacts.py` does not exist.

- [ ] **Step 3: Implement artifact service**

Create:

```python
class ArtifactWriter:
    def __init__(self, root: Path, *, run_id: int, node_id: str, columns: list[str], shard_size: int = 100_000): ...
    def append(self, file_row: int, rows: list[dict]) -> str: ...
    def close(self) -> dict: ...

def load_artifact(path: Path) -> dict: ...
def iter_artifact_rows(manifest: dict, *, start_file_row: int | None = None, end_file_row: int | None = None): ...
async def register_artifact_as_dataset(session, *, user_id, name, source_artifact, data_dir, run_id, node_id) -> Dataset: ...
```

`output_ref` format should be stable and parseable, for example `part-000001.jsonl:123`.

- [ ] **Step 4: Make output node register artifact**

In `runner.py`, change output `save_as_dataset` path to call `register_artifact_as_dataset(...)`. Keep legacy list path working until Task 8 changes per-row execution.

- [ ] **Step 5: Update run rows endpoint**

In `runs.py`, when `RunRow.output_ref` is set, read row data from artifact instead of `data_json`. Continue supporting legacy `data_json`.

- [ ] **Step 6: Run artifact tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_run_artifacts.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/services/run_artifacts.py backend/app/engine/runner.py backend/app/routers/runs.py
git add -f backend/tests/test_run_artifacts.py
git commit -m "feat(run): 增加节点输出 artifact 存储"
```

---

## Task 8: Streaming LLM/HTTP/QC Runner with Checkpoints

**Files:**
- Modify: `backend/app/engine/runner.py`
- Modify: `backend/app/engine/nodes.py`
- Test: `backend/tests/test_large_run_resume.py`
- Reference: spec sections 2.11, 2.12, 5

- [ ] **Step 1: Write resume tests**

Create `backend/tests/test_large_run_resume.py`:

```python
async def test_llm_node_skips_done_file_rows_on_resume(monkeypatch, session_factory, tmp_path):
    # First run completes row 2 and fails/stops before row 3.
    # Resume processes row 3 only.


async def test_done_row_is_not_called_twice(monkeypatch, session_factory, tmp_path):
    # Count fake llm.chat calls by file_row and assert completed file_row is absent on resume.


async def test_final_output_dedupes_by_file_row(monkeypatch, session_factory, tmp_path):
    # Simulate duplicate artifact write for one file_row and assert final output has one row.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_large_run_resume.py`

Expected: FAIL because runner still materializes list inputs and does not checkpoint by file row.

- [ ] **Step 3: Add input row iterator**

In `runner.py`, add a row source abstraction:

```python
class RowSource:
    columns: list[str]
    row_count: int
    async def iter_rows(self):
        yield file_row, row
```

Input nodes use dataset_store iterators. Upstream artifact nodes use run_artifacts iterators. Legacy `list[dict]` remains supported only inside compatibility wrappers.

- [ ] **Step 4: Change per-row nodes to queue workers**

Replace `_run_per_row_node(... inputs: list[dict])` with source-driven execution:

- Build `done_file_rows` from `RunRow.file_row`.
- Enqueue only unfinished `(file_row, row)`.
- Worker calls row coroutine.
- Append output to `ArtifactWriter`.
- Set `RunRow.file_row`, `status`, `output_ref`, tokens, and error.
- Update node state counts.

- [ ] **Step 5: Change LLM and HTTP node wrappers**

`_run_llm_node` and `_run_http_node` pass row data from `RowSource` and no longer require `len(inputs)` list materialization.

- [ ] **Step 6: Change QC node checkpointing**

QC can remain batch-compatible for small data during this task, but large inputs must not store final passed rows in one `RunRow.data_json`. Write passed rows to artifact and failed rows to `QcFailure` incrementally.

- [ ] **Step 7: Run resume tests and focused runner tests**

Run:

`cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_large_run_resume.py tests/test_runs.py`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/engine/runner.py backend/app/engine/nodes.py
git add -f backend/tests/test_large_run_resume.py
git commit -m "feat(run): LLM 跑数改为行级 checkpoint 续跑"
```

---

## Task 9: Auto Process Spill Algorithms

**Files:**
- Modify: `backend/app/engine/nodes.py`
- Modify: `backend/app/engine/runner.py`
- Test: `backend/tests/test_auto_process_spill.py`
- Reference: spec sections 2.13-2.15, 7

- [ ] **Step 1: Write spill tests**

Create tests:

```python
def test_reservoir_sample_is_bounded_and_seeded():
    # Same seed returns same sample, sample size remains n.


async def test_dedup_spills_to_sqlite(tmp_path):
    # Duplicate rows are removed using temp sqlite db, not Python set of full rows.


async def test_external_shuffle_is_seeded(tmp_path):
    # Same seed produces same order, different seed changes order.


async def test_filter_drop_rename_concat_stream_rows(tmp_path):
    # Stream operations preserve row order and do not require list input.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_auto_process_spill.py`

Expected: FAIL because spill helpers do not exist.

- [ ] **Step 3: Implement stream operation helpers**

Add:

```python
async def apply_operations_stream(row_iter, operations, *, seed, spill_dir):
    ...
```

Rules:

- `filter/drop/rename/concat/cast` stream directly.
- `sample` uses reservoir sampling.
- `shuffle` writes `random_key, seq, row_json` to temp SQLite and streams `ORDER BY random_key, seq`.
- `dedup` writes normalized key to temp SQLite with unique index.

- [ ] **Step 4: Connect auto_process node**

In runner, use stream helper when input is a `RowSource`. Keep `apply_operations_with_agent` fallback for user-code agent op; for very large inputs with agent op, write a clear node failure explaining that arbitrary Python agent op is not stream-safe until separately sandboxed for artifacts.

- [ ] **Step 5: Run spill tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_auto_process_spill.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/engine/nodes.py backend/app/engine/runner.py
git add -f backend/tests/test_auto_process_spill.py
git commit -m "feat(run): auto_process 增加 SQLite spill 算法"
```

---

## Task 10: Workflow Package Compatibility

**Files:**
- Modify: `backend/app/services/workflow_package.py`
- Test: `backend/tests/test_workflow_package.py`
- Reference: dataset store service and existing package design

- [ ] **Step 1: Add package tests for sharded datasets**

Extend `backend/tests/test_workflow_package.py`:

```python
async def test_export_package_reads_sharded_dataset(auth_client):
    # Upload dataset through new store, export workflow package, assert datasets/*.jsonl has all rows.


async def test_import_package_creates_sharded_dataset(auth_client):
    # Import .gfpkg and assert created dataset has manifest_json and no primary DatasetRow dependency.
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_workflow_package.py`

Expected: FAIL where package code still scans or creates `DatasetRow` directly.

- [ ] **Step 3: Update export package path**

Replace direct `DatasetRow` stream with dataset_store `iter_jsonl_lines(...)`.

- [ ] **Step 4: Update import package path**

When importing package dataset JSONL, write it as canonical shards and set manifest metadata. Do not populate `DatasetRow` except through a temporary compatibility branch if an existing test explicitly requires it.

- [ ] **Step 5: Run package tests**

Run: `cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_workflow_package.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add backend/app/services/workflow_package.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): 工作流包兼容分片数据集"
```

---

## Task 11: Spec Coverage, Regression, and Cleanup

**Files:**
- Create: `backend/tests/test_large_dataset_spec_coverage.py`
- Modify: existing tests only for exact compatibility fixes found during this task
- Reference: full spec

- [ ] **Step 1: Add spec coverage test**

Create a lightweight test that asserts the planned capability markers exist in code:

```python
def test_large_dataset_spec_coverage_markers():
    from app.services import dataset_store, dataset_crud, run_artifacts
    assert hasattr(dataset_store, "read_dataset_range")
    assert hasattr(dataset_store, "write_xlsx_export")
    assert hasattr(dataset_crud, "apply_dataset_operations")
    assert hasattr(run_artifacts, "ArtifactWriter")
```

Add behavior-level tests already created in earlier tasks to the coverage checklist in a module docstring.

- [ ] **Step 2: Run focused suite**

Run:

`cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_dataset_store.py tests/test_large_dataset_api.py tests/test_dataset_crud_versions.py tests/test_agent_dataset_tools.py tests/test_run_artifacts.py tests/test_large_run_resume.py tests/test_auto_process_spill.py tests/test_large_dataset_spec_coverage.py`

Expected: PASS.

- [ ] **Step 3: Run existing backend regression**

Run:

`cd "C:/Users/Admin/Desktop/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`

Expected: PASS.

- [ ] **Step 4: Run frontend build**

Run:

`cd "C:/Users/Admin/Desktop/GraphFlow/frontend" && npm run build`

Expected: PASS.

- [ ] **Step 5: Check git diff and ignored artifacts**

Run:

`cd "C:/Users/Admin/Desktop/GraphFlow" && git status --short`

Expected: only intended source, docs, and forced test files are changed; `.codegraph/` and `.idea/` remain untracked and unstaged.

- [ ] **Step 6: Commit final coverage cleanup**

```bash
cd "C:/Users/Admin/Desktop/GraphFlow"
git add -f backend/tests/test_large_dataset_spec_coverage.py
git commit -m "test(dataset): 增加超大数据集 spec 覆盖检查"
```

---

## Completion Gate

Before claiming implementation completion:

1. Re-read `docs/superpowers/specs/2026-06-22-large-dataset-optimization-design.md`.
2. Check every spec requirement maps to a task above.
3. Run backend full pytest.
4. Run frontend build.
5. Inspect `git status --short`.
6. Use `superpowers:verification-before-completion`.

This plan intentionally keeps the old `DatasetRow` table during rollout as a legacy compatibility and lazy-migration source. A later cleanup can remove primary dependence on it after production data has been migrated and package/export/run paths have been verified against shard manifests.
