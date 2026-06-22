import csv
import json
from collections.abc import Iterable
from pathlib import Path

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.columns import ordered_union
from app.models import Dataset, DatasetRow

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


async def create_dataset_from_upload(
    session: AsyncSession,
    *,
    user_id: int,
    filename: str,
    source_path: Path,
    data_dir: Path,
    shard_size: int = SHARD_SIZE,
) -> list[Dataset]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return [await _create_one_dataset(
            session, user_id=user_id, name=Path(filename).stem, original_filename=filename,
            original_format="csv", rows=_iter_csv_rows(source_path), columns=None,
            header_row=1, data_start_row=2, data_dir=data_dir, shard_size=shard_size,
            file_path=str(source_path))]
    if suffix == ".jsonl":
        return [await _create_one_dataset(
            session, user_id=user_id, name=Path(filename).stem, original_filename=filename,
            original_format="jsonl", rows=_iter_jsonl_rows(source_path), columns=None,
            header_row=None, data_start_row=1, data_dir=data_dir, shard_size=shard_size,
            file_path=str(source_path))]
    if suffix == ".json":
        return [await _create_one_dataset(
            session, user_id=user_id, name=Path(filename).stem, original_filename=filename,
            original_format="jsonl", rows=_iter_json_rows(source_path), columns=None,
            header_row=None, data_start_row=1, data_dir=data_dir, shard_size=shard_size,
            file_path=str(source_path))]
    if suffix in (".xlsx", ".xls"):
        return await _create_excel_datasets(
            session, user_id=user_id, filename=filename, source_path=source_path,
            data_dir=data_dir, shard_size=shard_size)
    raise ValueError(f"不支持的文件格式: {suffix}")


async def _create_excel_datasets(
    session: AsyncSession,
    *,
    user_id: int,
    filename: str,
    source_path: Path,
    data_dir: Path,
    shard_size: int,
) -> list[Dataset]:
    stem = Path(filename).stem
    wb = load_workbook(source_path, read_only=True, data_only=True)
    items: list[tuple[str, list[str], dict, Iterable[dict]]] = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        try:
            raw_header = next(rows)
        except StopIteration:
            continue
        headers = [_cell_to_str(v) for v in raw_header]
        records = _rows_from_values(headers, rows)
        first = next(records, None)
        if first is not None:
            items.append((ws.title, headers, first, records))
    datasets: list[Dataset] = []
    for sheet_name, headers, first, records in items:
        name = stem if len(items) == 1 else f"{stem}-{sheet_name}"
        datasets.append(await _create_one_dataset(
            session, user_id=user_id, name=name, original_filename=filename,
            original_format="xlsx", rows=_prepend(first, records), columns=headers,
            header_row=1, data_start_row=2, data_dir=data_dir, shard_size=shard_size,
            file_path=str(source_path)))
    return datasets


async def _create_one_dataset(
    session: AsyncSession,
    *,
    user_id: int,
    name: str,
    original_filename: str,
    original_format: str,
    rows: Iterable[dict],
    columns: list[str] | None,
    header_row: int | None,
    data_start_row: int,
    data_dir: Path,
    shard_size: int,
    file_path: str,
) -> Dataset:
    ds = Dataset(
        user_id=user_id, name=name, source="upload", original_filename=original_filename,
        original_format=original_format, file_path=file_path, row_count=0,
        columns_json=json.dumps(columns or [], ensure_ascii=False), status="importing",
        header_row=header_row, data_start_row=data_start_row, total_rows_including_header=0,
    )
    session.add(ds)
    await session.flush()
    manifest, final_columns, row_count = _write_shards(
        rows, data_dir=data_dir, user_id=user_id, dataset_id=ds.id, version=ds.version,
        shard_size=shard_size, columns=columns, header_row=header_row,
        data_start_row=data_start_row)
    manifest["original_format"] = original_format
    ds.manifest_json = dump_manifest(manifest)
    ds.columns_json = json.dumps(final_columns, ensure_ascii=False)
    ds.row_count = row_count
    ds.imported_rows = row_count
    ds.total_rows_including_header = visible_total(row_count, header_row)
    ds.status = "ready"
    await session.commit()
    return ds


def _write_shards(
    rows: Iterable[dict],
    *,
    data_dir: Path,
    user_id: int,
    dataset_id: int,
    version: int,
    shard_size: int,
    columns: list[str] | None,
    header_row: int | None,
    data_start_row: int,
) -> tuple[dict, list[str], int]:
    root = dataset_root(data_dir, user_id, dataset_id, version)
    root.mkdir(parents=True, exist_ok=True)
    shards: list[dict] = []
    current = None
    row_count = 0
    shard_rows = 0
    shard_no = 0
    seen_columns = list(columns or [])

    def open_shard():
        nonlocal current, shard_no, shard_rows
        shard_no += 1
        shard_rows = 0
        path = root / f"part-{shard_no:06d}.jsonl"
        current = path.open("w", encoding="utf-8", newline="\n")
        shards.append({
            "path": path.relative_to(data_dir).as_posix(),
            "start_data_idx": row_count,
            "start_file_row": data_start_row + row_count,
            "row_count": 0,
            "columns": seen_columns,
        })

    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("数据行必须是 JSON object")
            if current is None or shard_rows >= shard_size:
                if current is not None:
                    current.close()
                open_shard()
            for key in row:
                if key not in seen_columns:
                    seen_columns.append(key)
            current.write(json.dumps(row, ensure_ascii=False) + "\n")
            shard_rows += 1
            row_count += 1
            shards[-1]["row_count"] = shard_rows
            shards[-1]["columns"] = list(seen_columns)
    finally:
        if current is not None:
            current.close()
    manifest = {
        "original_format": "",
        "canonical_format": "jsonl",
        "version": version,
        "version_of_dataset_id": None,
        "header_row": header_row,
        "data_start_row": data_start_row,
        "shard_size": shard_size,
        "shards": shards,
    }
    return manifest, seen_columns, row_count


async def ensure_dataset_materialized(session: AsyncSession, ds: Dataset, data_dir: Path) -> Dataset:
    if load_manifest(ds).get("shards"):
        return ds
    recs = (await session.execute(
        select(DatasetRow).where(DatasetRow.dataset_id == ds.id).order_by(DatasetRow.idx)
    )).scalars().all()
    if not recs:
        return ds
    suffix = Path(ds.original_filename or "").suffix.lower()
    header_row = 1 if suffix in (".csv", ".xlsx", ".xls") else None
    data_start_row = 2 if header_row is not None else 1
    original_format = suffix.lstrip(".") if suffix else "jsonl"
    rows = (json.loads(r.data_json) for r in recs)
    manifest, columns, row_count = _write_shards(
        rows, data_dir=data_dir, user_id=ds.user_id, dataset_id=ds.id, version=ds.version,
        shard_size=SHARD_SIZE, columns=json.loads(ds.columns_json or "[]"),
        header_row=header_row, data_start_row=data_start_row)
    manifest["original_format"] = original_format
    ds.manifest_json = dump_manifest(manifest)
    ds.original_format = original_format
    ds.header_row = header_row
    ds.data_start_row = data_start_row
    ds.row_count = row_count
    ds.imported_rows = row_count
    ds.total_rows_including_header = visible_total(row_count, header_row)
    ds.columns_json = json.dumps(columns, ensure_ascii=False)
    ds.status = "ready"
    await session.commit()
    return ds


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
    ds = await ensure_dataset_materialized(session, ds, data_dir)
    selected = _select_columns(ds, columns)
    rows: list[dict] = []
    truncated = False
    char_budget = max_chars if max_chars is not None else MAX_AGENT_CHARS
    used_chars = 0

    def append_row(row: dict) -> bool:
        nonlocal used_chars, truncated
        if max_rows is not None and len(rows) >= max_rows:
            truncated = True
            return False
        size = len(json.dumps(row, ensure_ascii=False))
        if char_budget is not None and rows and used_chars + size > char_budget:
            truncated = True
            return False
        rows.append(row)
        used_chars += size
        return True

    if ds.header_row is not None and start_row <= ds.header_row <= end_row:
        if not append_row({"__row_type": "header", "columns": selected}):
            return _range_payload(ds, start_row, end_row, selected, rows, truncated)

    for file_row, row in _iter_dataset_rows(ds, data_dir, start_row=start_row, end_row=end_row):
        if file_row < ds.data_start_row:
            continue
        if not append_row(_project(row, selected)):
            break
    return _range_payload(ds, start_row, end_row, selected, rows, truncated)


async def iter_jsonl_lines(session: AsyncSession, ds: Dataset, data_dir: Path):
    ds = await ensure_dataset_materialized(session, ds, data_dir)
    for _, row in _iter_dataset_rows(ds, data_dir):
        yield json.dumps(row, ensure_ascii=False) + "\n"


async def write_jsonl_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ds = await ensure_dataset_materialized(session, ds, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for _, row in _iter_dataset_rows(ds, data_dir):
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


async def write_csv_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ds = await ensure_dataset_materialized(session, ds, data_dir)
    columns = json.loads(ds.columns_json or "[]")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for _, row in _iter_dataset_rows(ds, data_dir):
            writer.writerow(row)
    return path


async def write_xlsx_export(session: AsyncSession, ds: Dataset, data_dir: Path, path: Path) -> Path:
    ds = await ensure_dataset_materialized(session, ds, data_dir)
    columns = json.loads(ds.columns_json or "[]")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = None
    sheet_no = 0
    sheet_rows = 0
    for _, row in _iter_dataset_rows(ds, data_dir):
        if ws is None or sheet_rows >= EXCEL_MAX_DATA_ROWS_PER_SHEET:
            sheet_no += 1
            ws = wb.create_sheet("data" if sheet_no == 1 else f"data_{sheet_no}")
            ws.append(columns)
            sheet_rows = 0
        ws.append([row.get(col, "") for col in columns])
        sheet_rows += 1
    if ws is None:
        ws = wb.create_sheet("data")
        ws.append(columns)
    wb.save(path)
    return path


def _range_payload(ds: Dataset, start_row: int, end_row: int, columns: list[str],
                   rows: list[dict], truncated: bool) -> dict:
    return {
        "dataset_id": ds.id,
        "total": ds.row_count,
        "total_rows": ds.row_count,
        "total_rows_including_header": ds.total_rows_including_header,
        "header_row": ds.header_row,
        "data_start_row": ds.data_start_row,
        "start_row": start_row,
        "end_row": end_row,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
    }


def _iter_dataset_rows(ds: Dataset, data_dir: Path, *,
                       start_row: int | None = None,
                       end_row: int | None = None):
    manifest = load_manifest(ds)
    for shard in manifest.get("shards", []):
        shard_start = int(shard["start_file_row"])
        shard_end = shard_start + int(shard["row_count"]) - 1
        if start_row is not None and shard_end < start_row:
            continue
        if end_row is not None and shard_start > end_row:
            continue
        path = Path(shard["path"])
        if not path.is_absolute():
            path = data_dir / path
        with path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                file_row = shard_start + i
                if start_row is not None and file_row < start_row:
                    continue
                if end_row is not None and file_row > end_row:
                    break
                if line.strip():
                    yield file_row, json.loads(line)


def _iter_csv_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield {k: "" if v is None else v for k, v in row.items()}


def _iter_jsonl_rows(path: Path):
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            if line.strip():
                obj = json.loads(line, parse_constant=lambda _v: None)
                if not isinstance(obj, dict):
                    raise ValueError("JSONL 每行必须是 JSON object")
                yield obj


def _iter_json_rows(path: Path):
    data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda _v: None)
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("JSON 内容必须是 object 或 object 列表")
        yield row


def _rows_from_values(headers: list[str], rows):
    for values in rows:
        if values is None:
            continue
        row = {headers[i]: _cell_to_value(values[i]) if i < len(values) else ""
               for i in range(len(headers))}
        if any(v != "" for v in row.values()):
            yield row


def _prepend(first: dict, rows: Iterable[dict]):
    yield first
    yield from rows


def _cell_to_str(value) -> str:
    return "" if value is None else str(value)


def _cell_to_value(value):
    return "" if value is None else value


def _select_columns(ds: Dataset, columns: list[str] | None) -> list[str]:
    available = json.loads(ds.columns_json or "[]")
    if columns is None:
        return available
    return [c for c in columns if c in available]


def _project(row: dict, columns: list[str]) -> dict:
    return {col: row.get(col, "") for col in columns}
