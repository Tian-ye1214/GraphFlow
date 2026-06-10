import json
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import delete as sa_delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_session
from app.models import Dataset, DatasetRow, User
from app.services.file_parse import parse_file, union_columns

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


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
                         file_path: str = "") -> Dataset:
    """供上传与（后续）运行结果保存共用。"""
    ds = Dataset(user_id=user_id, name=name, source=source, original_filename=original_filename,
                 file_path=file_path,
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
        try:
            rows = parse_file(f.filename, content)
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"{f.filename} 解析失败: {e}")
        # 仅取文件名末段并清洗非法字符，杜绝路径穿越；uuid 前缀保证唯一
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", Path(f.filename).name)
        upload_dir = settings.data_dir / "uploads" / str(user.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{uuid4().hex[:8]}_{safe_name}"
        file_path.write_bytes(content)
        ds = await create_dataset(session, user.id, Path(f.filename).stem, rows,
                                  original_filename=f.filename, file_path=str(file_path))
        results.append(_out(ds))
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


@router.delete("/{ds_id}")
async def delete_dataset(ds_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
    if ds.file_path:
        Path(ds.file_path).unlink(missing_ok=True)
    await session.delete(ds)
    await session.commit()
    return {"ok": True}
