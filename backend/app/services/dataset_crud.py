import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dataset
from app.services.dataset_store import (
    SHARD_SIZE,
    _iter_dataset_rows,
    _write_shards,
    dump_manifest,
    ensure_dataset_materialized,
    visible_total,
)


class DatasetCrudError(ValueError):
    pass


async def apply_dataset_operations(
    session: AsyncSession,
    *,
    source: Dataset,
    user_id: int,
    data_dir: Path,
    operations: list[dict],
) -> Dataset:
    if not operations:
        raise DatasetCrudError("operations must not be empty")
    source = await ensure_dataset_materialized(session, source, data_dir)
    total_rows = source.total_rows_including_header or visible_total(
        source.row_count, source.header_row)
    source_columns = json.loads(source.columns_json or "[]")
    plan = _build_plan(source, source_columns, total_rows, operations)
    root_id = source.version_of_dataset_id or source.id
    version = await _next_version(session, user_id, root_id)
    root = await session.get(Dataset, root_id)
    base_name = root.name if root is not None else source.name

    new_ds = Dataset(
        user_id=user_id,
        name=f"{base_name} v{version}",
        source=source.source,
        original_filename=source.original_filename,
        file_path=source.file_path,
        row_count=0,
        columns_json=json.dumps(plan["columns"], ensure_ascii=False),
        status="importing",
        imported_rows=0,
        original_format=source.original_format,
        version=version,
        version_of_dataset_id=root_id,
        header_row=source.header_row,
        data_start_row=source.data_start_row,
        total_rows_including_header=0,
    )
    session.add(new_ds)
    await session.flush()

    rows = _apply_row_plan(source, data_dir, plan)
    manifest, columns, row_count = _write_shards(
        rows,
        data_dir=data_dir,
        user_id=user_id,
        dataset_id=new_ds.id,
        version=version,
        shard_size=SHARD_SIZE,
        columns=plan["columns"],
        header_row=source.header_row,
        data_start_row=source.data_start_row,
    )
    manifest["original_format"] = source.original_format
    manifest["version_of_dataset_id"] = root_id
    new_ds.manifest_json = dump_manifest(manifest)
    new_ds.columns_json = json.dumps(columns, ensure_ascii=False)
    new_ds.row_count = row_count
    new_ds.imported_rows = row_count
    new_ds.total_rows_including_header = visible_total(row_count, source.header_row)
    new_ds.status = "ready"
    await session.commit()
    return new_ds


async def _next_version(session: AsyncSession, user_id: int, root_id: int) -> int:
    max_version = (await session.execute(
        select(func.max(Dataset.version)).where(
            Dataset.user_id == user_id,
            or_(Dataset.id == root_id, Dataset.version_of_dataset_id == root_id),
        )
    )).scalar_one()
    return int(max_version or 1) + 1


def _build_plan(source: Dataset, source_columns: list[str], total_rows: int,
                operations: list[dict]) -> dict:
    columns = list(source_columns)
    column_ops: list[dict] = []
    delete_ranges: list[tuple[int, int]] = []
    replace_ranges: dict[int, tuple[int, list[dict]]] = {}
    inserts: dict[int, list[dict]] = defaultdict(list)

    for op in operations:
        if not isinstance(op, dict):
            raise DatasetCrudError("operation must be an object")
        kind = op.get("op")
        if kind == "delete_rows":
            start, end = _range(op, total_rows, source)
            delete_ranges.append((start, end))
        elif kind == "replace_rows":
            rows = _rows(op.get("rows"), "rows")
            start = _positive_int(op.get("start_row"), "start_row")
            end = _positive_int(op.get("end_row"), "end_row") if op.get("end_row") is not None else start
            if end < start:
                raise DatasetCrudError("end_row must be >= start_row")
            _validate_data_range(source, total_rows, start, end)
            if start in replace_ranges:
                raise DatasetCrudError("row operations overlap")
            replace_ranges[start] = (end, rows)
        elif kind == "insert_rows":
            before = _positive_int(op.get("before_row"), "before_row")
            rows = _rows(op.get("rows"), "rows")
            _validate_insert_row(source, total_rows, before)
            inserts[before].extend(rows)
        elif kind == "rename_column":
            src = _name(op.get("from"), "from")
            dst = _name(op.get("to"), "to")
            if src not in columns:
                raise DatasetCrudError(f"unknown column: {src}")
            if dst != src and dst in columns:
                raise DatasetCrudError(f"column already exists: {dst}")
            columns = [dst if col == src else col for col in columns]
            column_ops.append({"op": kind, "from": src, "to": dst})
        elif kind in {"drop_column", "drop_columns"}:
            names = _drop_names(op)
            for name in names:
                if name not in columns:
                    raise DatasetCrudError(f"unknown column: {name}")
                columns = [col for col in columns if col != name]
            column_ops.append({"op": "drop_columns", "columns": names})
        elif kind == "add_constant_column":
            name = _name(op.get("name"), "name")
            if name in columns:
                raise DatasetCrudError(f"column already exists: {name}")
            columns.append(name)
            column_ops.append({"op": kind, "name": name, "value": op.get("value", "")})
        else:
            raise DatasetCrudError(f"unknown operation: {kind}")

    _validate_non_overlapping(delete_ranges + [
        (start, end_rows[0]) for start, end_rows in replace_ranges.items()
    ])
    return {
        "columns": columns,
        "column_ops": column_ops,
        "delete_ranges": sorted(delete_ranges),
        "replace_ranges": replace_ranges,
        "inserts": dict(inserts),
        "total_rows": total_rows,
    }


def _apply_row_plan(source: Dataset, data_dir: Path, plan: dict) -> Iterable[dict]:
    delete_ranges = plan["delete_ranges"]
    replace_ranges = plan["replace_ranges"]
    inserts = plan["inserts"]
    skip_until = 0

    for file_row, row in _iter_dataset_rows(source, data_dir):
        for inserted in inserts.get(file_row, []):
            yield _apply_column_ops(inserted, plan)
        if file_row <= skip_until:
            continue
        if file_row in replace_ranges:
            end_row, rows = replace_ranges[file_row]
            for replacement in rows:
                yield _apply_column_ops(replacement, plan)
            skip_until = end_row
            continue
        if _in_ranges(file_row, delete_ranges):
            continue
        yield _apply_column_ops(row, plan)

    append_row = plan["total_rows"] + 1
    for inserted in inserts.get(append_row, []):
        yield _apply_column_ops(inserted, plan)


def _apply_column_ops(row: dict, plan: dict) -> dict:
    out = dict(row)
    for op in plan["column_ops"]:
        kind = op["op"]
        if kind == "rename_column":
            src = op["from"]
            dst = op["to"]
            if src in out and dst != src:
                out[dst] = out.pop(src)
        elif kind == "drop_columns":
            for name in op["columns"]:
                out.pop(name, None)
        elif kind == "add_constant_column":
            out[op["name"]] = op["value"]
    return {col: out.get(col, "") for col in plan["columns"]}


def _range(op: dict, total_rows: int, source: Dataset) -> tuple[int, int]:
    start = _positive_int(op.get("start_row"), "start_row")
    end = _positive_int(op.get("end_row"), "end_row")
    if end < start:
        raise DatasetCrudError("end_row must be >= start_row")
    _validate_data_range(source, total_rows, start, end)
    return start, end


def _validate_data_range(source: Dataset, total_rows: int, start: int, end: int) -> None:
    if source.header_row is not None and start <= source.header_row <= end:
        raise DatasetCrudError("header row cannot be edited as data")
    data_start = source.data_start_row or 1
    if start < data_start or end > total_rows:
        raise DatasetCrudError("row range out of bounds")


def _validate_insert_row(source: Dataset, total_rows: int, before_row: int) -> None:
    data_start = source.data_start_row or 1
    if before_row < data_start or before_row > total_rows + 1:
        raise DatasetCrudError("insert row out of bounds")


def _validate_non_overlapping(ranges: list[tuple[int, int]]) -> None:
    prev_end = 0
    for start, end in sorted(ranges):
        if start <= prev_end:
            raise DatasetCrudError("row operations overlap")
        prev_end = end


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DatasetCrudError(f"{name} must be a positive integer")
    return value


def _rows(value, name: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise DatasetCrudError(f"{name} must be a list of objects")
    return value


def _name(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetCrudError(f"{name} must be a non-empty string")
    return value.strip()


def _drop_names(op: dict) -> list[str]:
    if op.get("op") == "drop_column":
        return [_name(op.get("name"), "name")]
    columns = op.get("columns")
    if not isinstance(columns, list):
        raise DatasetCrudError("columns must be a list")
    return [_name(name, "columns") for name in columns]


def _in_ranges(file_row: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= file_row <= end for start, end in ranges)
