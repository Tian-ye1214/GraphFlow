import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete as sa_delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.events import publish
from app.models import Dataset, DatasetRow, User
from app.services.dataset_store import (
    create_dataset_from_upload,
    ensure_dataset_materialized,
    iter_jsonl_lines,
    read_dataset_range,
    write_csv_export,
    write_xlsx_export,
)
from app.services.file_parse import union_columns

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(name: str) -> str:
    cleaned = _ILLEGAL_FN.sub("_", name).strip(" .")[:200].strip(" .")
    return cleaned or "untitled"


def _out(ds: Dataset) -> dict:
    total_rows_including_header = (
        ds.total_rows_including_header
        or ds.row_count + (1 if ds.header_row is not None else 0)
    )
    return {
        "id": ds.id,
        "name": ds.name,
        "source": ds.source,
        "original_filename": ds.original_filename,
        "row_count": ds.row_count,
        "columns": json.loads(ds.columns_json or "[]"),
        "created_at": ds.created_at.isoformat(),
        "status": ds.status,
        "imported_rows": ds.imported_rows,
        "original_format": ds.original_format,
        "version": ds.version,
        "version_of_dataset_id": ds.version_of_dataset_id,
        "header_row": ds.header_row,
        "data_start_row": ds.data_start_row,
        "total_rows_including_header": total_rows_including_header,
    }


def _columns_arg(columns: str | None) -> list[str] | None:
    if not columns:
        return None
    selected = [col.strip() for col in columns.split(",") if col.strip()]
    return selected or None


def _requested_export_format(ds: Dataset, requested: str) -> str:
    if requested != "original":
        return requested
    original = (
        ds.original_format
        or Path(ds.original_filename or "").suffix.lstrip(".")
        or "jsonl"
    ).lower()
    if original == "json":
        return "jsonl"
    if original == "xls":
        return "xlsx"
    if original in {"csv", "jsonl", "xlsx"}:
        return original
    return "jsonl"


def _attachment_headers(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


async def _get_owned(ds_id: int, user: User, session: AsyncSession) -> Dataset:
    ds = await session.get(Dataset, ds_id)
    if ds is None or ds.user_id != user.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


async def create_dataset(
    session: AsyncSession,
    user_id: int,
    name: str,
    rows: list[dict],
    source: str = "upload",
    original_filename: str = "",
    file_path: str = "",
    run_id: int | None = None,
    node_id: str | None = None,
) -> Dataset:
    """Compatibility path for runner outputs until run artifacts replace in-memory rows."""
    columns = union_columns(rows)
    ds = None
    if run_id is not None and node_id is not None:
        ds = (await session.execute(select(Dataset).where(
            Dataset.run_id == run_id,
            Dataset.node_id == node_id,
            Dataset.user_id == user_id,
        ))).scalars().first()

    if ds is not None:
        await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
        ds.name = name
        ds.row_count = len(rows)
        ds.columns_json = json.dumps(columns, ensure_ascii=False)
        ds.status = "ready"
        ds.imported_rows = len(rows)
        ds.original_format = ds.original_format or "jsonl"
        ds.header_row = None
        ds.data_start_row = 1
        ds.total_rows_including_header = len(rows)
        ds.manifest_json = "{}"
    else:
        ds = Dataset(
            user_id=user_id,
            name=name,
            source=source,
            original_filename=original_filename,
            file_path=file_path,
            run_id=run_id,
            node_id=node_id,
            row_count=len(rows),
            columns_json=json.dumps(columns, ensure_ascii=False),
            status="ready",
            imported_rows=len(rows),
            original_format="jsonl",
            header_row=None,
            data_start_row=1,
            total_rows_including_header=len(rows),
        )
        session.add(ds)
        await session.flush()

    if rows:
        await session.execute(insert(DatasetRow), [
            {"dataset_id": ds.id, "idx": i, "data_json": json.dumps(row, ensure_ascii=False)}
            for i, row in enumerate(rows)
        ])
    await session.commit()
    return ds


@router.post("/upload")
async def upload(
    files: list[UploadFile],
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    results = []
    for upload_file in files:
        original_name = upload_file.filename or "upload"
        safe_name = _safe_filename(Path(original_name).name)
        upload_dir = settings.data_dir / "uploads" / str(user.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_id = uuid4().hex[:8]
        tmp_path = upload_dir / f".{upload_id}_{safe_name}.tmp"
        file_path = upload_dir / f"{upload_id}_{safe_name}"

        try:
            with tmp_path.open("wb") as out:
                while chunk := await upload_file.read(1024 * 1024):
                    out.write(chunk)
            tmp_path.replace(file_path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=f"{original_name} save failed",
            ) from exc

        try:
            datasets = await create_dataset_from_upload(
                session,
                user_id=user.id,
                filename=original_name,
                source_path=file_path,
                data_dir=settings.data_dir,
            )
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            await session.rollback()
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=f"{original_name} parse failed: {exc}",
            ) from exc

        for ds in datasets:
            results.append(_out(ds))
            publish(user.id, "dataset", ds.id)
    return results


@router.get("")
async def list_datasets(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.id.desc())
    )).scalars().all()
    return [_out(ds) for ds in rows]


@router.get("/{ds_id}/rows")
async def dataset_rows(
    ds_id: int,
    page: int = 1,
    page_size: int = 20,
    start_row: int | None = None,
    end_row: int | None = None,
    columns: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ds = await _get_owned(ds_id, user, session)
    if start_row is not None or end_row is not None:
        if start_row is None or end_row is None or start_row < 1 or end_row < start_row:
            raise HTTPException(status_code=422, detail="Invalid start_row/end_row")
        return await read_dataset_range(
            session,
            ds,
            data_dir=settings.data_dir,
            start_row=start_row,
            end_row=end_row,
            columns=_columns_arg(columns),
        )

    if page < 1 or page_size < 1:
        raise HTTPException(status_code=422, detail="Invalid page/page_size")
    start = (ds.data_start_row or 1) + (page - 1) * page_size
    end = start + page_size - 1
    payload = await read_dataset_range(
        session,
        ds,
        data_dir=settings.data_dir,
        start_row=start,
        end_row=end,
    )
    return {"total": payload["total"], "rows": payload["rows"]}


@router.get("/{ds_id}/export")
async def export_dataset(
    ds_id: int,
    format: Literal["original", "jsonl", "csv", "xlsx"] = "original",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ds = await _get_owned(ds_id, user, session)
    export_format = _requested_export_format(ds, format)
    safe = _safe_filename(ds.name)
    filename = f"{safe}.{export_format}"

    try:
        ds = await ensure_dataset_materialized(session, ds, settings.data_dir)
        if export_format == "jsonl":
            return StreamingResponse(
                iter_jsonl_lines(session, ds, settings.data_dir),
                media_type="application/x-ndjson",
                headers=_attachment_headers(filename),
            )

        path = settings.data_dir / "exports" / f"{uuid4().hex[:8]}_{filename}"
        if export_format == "csv":
            path = await write_csv_export(session, ds, settings.data_dir, path)
            return FileResponse(path, filename=filename, media_type="text/csv; charset=utf-8")

        path = await write_xlsx_export(session, ds, settings.data_dir, path)
        return FileResponse(
            path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except OSError as exc:
        raise HTTPException(status_code=422, detail="Export failed") from exc


@router.delete("/{ds_id}")
async def delete_dataset(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ds = await _get_owned(ds_id, user, session)
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
    if ds.file_path:
        Path(ds.file_path).unlink(missing_ok=True)
    await session.delete(ds)
    await session.commit()
    publish(user.id, "dataset", ds_id)
    return {"ok": True}
