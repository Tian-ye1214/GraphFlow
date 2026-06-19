import asyncio
import json
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete as sa_delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.events import publish
from app.models import Dataset, DatasetRow, User
from app.services.export import export_rows
from app.services.file_parse import parse_file, parse_sheets, union_columns

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

# 非法文件名字符：Windows 保留符 + 控制字符(\x00-\x1f，可经多 sheet Excel 的 sheet 名 \r\n 注入)。
# 不清掉控制字符会让 write_text/Content-Disposition 抛 OSError[Errno22] 逃逸成 500。
_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(name: str) -> str:
    cleaned = _ILLEGAL_FN.sub("_", name).strip(" .")
    return cleaned or "untitled"


def _out(ds: Dataset) -> dict:
    return {
        "id": ds.id, "name": ds.name, "source": ds.source,
        "original_filename": ds.original_filename, "row_count": ds.row_count,
        "columns": json.loads(ds.columns_json), "created_at": ds.created_at.isoformat(),
    }


async def _get_owned(ds_id: int, user: User, session: AsyncSession) -> Dataset:
    ds = await session.get(Dataset, ds_id)
    if ds is None or ds.user_id != user.id:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return ds


async def create_dataset(session: AsyncSession, user_id: int, name: str, rows: list[dict],
                         source: str = "upload", original_filename: str = "",
                         file_path: str = "", run_id: int | None = None) -> Dataset:
    """供上传与运行结果保存共用。传 run_id（save_as_dataset）时按 (run_id, name) 幂等：
    同一 run 重算同名 output 覆盖更新，而非产生重复数据集（rerun-failed 会重算下游 output）。"""
    ds = None
    if run_id is not None:
        ds = (await session.execute(select(Dataset).where(
            Dataset.run_id == run_id, Dataset.name == name, Dataset.user_id == user_id))).scalars().first()
    if ds is not None:                       # 覆盖更新：清旧行、重置 schema/计数
        await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
        ds.row_count = len(rows)
        ds.columns_json = json.dumps(union_columns(rows), ensure_ascii=False)
    else:
        ds = Dataset(user_id=user_id, name=name, source=source, original_filename=original_filename,
                     file_path=file_path, run_id=run_id,
                     row_count=len(rows), columns_json=json.dumps(union_columns(rows), ensure_ascii=False))
        session.add(ds)
        await session.flush()
    if rows:
        await session.execute(insert(DatasetRow), [
            {"dataset_id": ds.id, "idx": i, "data_json": json.dumps(r, ensure_ascii=False)}
            for i, r in enumerate(rows)
        ])
    await session.commit()
    return ds


@router.post("/upload")
async def upload(files: list[UploadFile], user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    results = []
    for f in files:
        content = await f.read()
        suffix = Path(f.filename).suffix.lower()
        try:  # Excel 多 sheet → 每个非空 sheet 一个数据集；其余 → 单个数据集
            parsed = (parse_sheets(f.filename, content) if suffix in (".xlsx", ".xls")
                      else [(Path(f.filename).stem, parse_file(f.filename, content))])
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"{f.filename} 解析失败: {e}")
        # 仅取文件名末段并清洗非法字符，杜绝路径穿越；uuid 前缀保证唯一
        safe_name = _safe_filename(Path(f.filename).name)
        upload_dir = settings.data_dir / "uploads" / str(user.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{uuid4().hex[:8]}_{safe_name}"
        file_path.write_bytes(content)
        for name, rows in parsed:
            ds = await create_dataset(session, user.id, name, rows,
                                      original_filename=f.filename, file_path=str(file_path))
            results.append(_out(ds))
            publish(user.id, "dataset", ds.id)
    return results


@router.get("")
async def list_datasets(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.id.desc())
    )).scalars().all()
    return [_out(d) for d in rows]


@router.get("/{ds_id}/rows")
async def dataset_rows(ds_id: int, page: int = 1, page_size: int = 20,
                       user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    stmt = (select(DatasetRow).where(DatasetRow.dataset_id == ds.id)
            .order_by(DatasetRow.idx).offset((page - 1) * page_size).limit(page_size))
    rows = (await session.execute(stmt)).scalars().all()
    return {"total": ds.row_count, "rows": [json.loads(r.data_json) for r in rows]}


@router.get("/{ds_id}/export")
async def export_dataset(ds_id: int, format: Literal["jsonl", "csv", "xlsx"] = "jsonl",
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    recs = (await session.execute(select(DatasetRow).where(
        DatasetRow.dataset_id == ds.id).order_by(DatasetRow.idx))).scalars().all()
    rows = [json.loads(r.data_json) for r in recs]
    safe = _safe_filename(ds.name)   # 与 upload 同款清洗，杜绝路径穿越/控制字符 → OSError 500
    filename = f"{safe}.{format}"
    path = await asyncio.to_thread(
        export_rows, rows, format, settings.data_dir / "exports" / filename)
    return FileResponse(path, filename=filename)


@router.delete("/{ds_id}")
async def delete_dataset(ds_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
    if ds.file_path:
        Path(ds.file_path).unlink(missing_ok=True)
    await session.delete(ds)
    await session.commit()
    publish(user.id, "dataset", ds_id)
    return {"ok": True}
