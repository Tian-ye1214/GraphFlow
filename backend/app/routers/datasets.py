import json
import os
import re
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl.utils.exceptions import IllegalCharacterError
from sqlalchemy import delete as sa_delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth import get_current_user
from app.config import settings
from app.db import get_session, get_session_factory
from app.events import publish
from app.models import Dataset, DatasetRow, User
from app.services.dataset_crud import DatasetCrudError, apply_dataset_operations
from app.services.dataset_store import (
    dataset_root,
    detect_upload_structure,
    ensure_dataset_materialized,
    iter_csv_lines,
    iter_jsonl_lines,
    read_dataset_range,
    write_xlsx_export,
)
from app.services.file_parse import union_columns
from app.services.ingest_manager import ingest_manager

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

# /rows 单请求行数硬上限：人类分页/范围读取已关字符预算，须有行数顶防超大 page_size/范围把整库读进内存。
MAX_ROWS_PER_REQUEST = 5000

SUPPORTED_SUFFIXES = {".csv", ".jsonl", ".json", ".xlsx", ".xls"}

# 内置小样本：一键给新用户灌入可立即跑的演示数据，不依赖任何外部文件。
# 走 create_dataset 内存路径建成 ready（数据极小），/rows、/export 首读时由 ensure_dataset_materialized 落分片。
SAMPLE_DATASETS: list[tuple[str, list[dict]]] = [
    ("示例-中文短句", [{"q": s} for s in [
        "今天天气怎么样", "帮我写一首关于春天的诗", "什么是机器学习",
        "推荐几本科幻小说", "如何提高睡眠质量", "解释一下相对论",
        "用一句话介绍北京", "番茄炒蛋怎么做", "为什么天空是蓝色的",
        "如何缓解工作压力", "介绍一下长城的历史", "怎样学好英语",
        "什么是区块链", "推荐一部好看的电影", "如何养成阅读习惯",
        "光合作用的原理是什么", "怎么挑选一台笔记本电脑", "简述唐朝的兴衰",
        "如何制定健身计划", "什么是人工智能"]]),
    ("示例-待分类评论", [{"text": s} for s in [
        "这家餐厅的菜真好吃，下次还来", "服务态度太差了，再也不来了",
        "物流速度很快，包装也很好", "质量一般，跟描述不太一样",
        "性价比超高，强烈推荐", "用了一周就坏了，很失望",
        "客服很耐心，解决了我的问题", "价格偏贵，但东西确实不错",
        "发货太慢了，等了半个月", "颜色和图片有色差，有点难受",
        "手感很棒，做工精细", "完全是智商税，不要买",
        "界面简洁，操作流畅", "广告太多，体验很差",
        "音质出乎意料地好", "电池续航很拉胯",
        "包装精美，适合送礼", "拍照效果一般般",
        "性能强劲，玩游戏很爽", "系统经常卡顿，闹心"]]),
]

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
        "import_error": ds.import_error,
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
    # 单一路径不分大小文件：同步落盘+廉价校验+结构探测建占位行(importing)，逐行写分片交后台摄入。
    # 端点秒级返回 importing 占位，前端轮询/SSE 看 status→ready/failed（深层解析错落 failed，不再同步 422）。
    results = []
    factory = get_session_factory()
    for upload_file in files:
        original_name = upload_file.filename or "upload"
        safe_name = _safe_filename(Path(original_name).name)
        suffix = Path(original_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(status_code=422,
                                detail=f"{original_name}: 不支持的文件格式 {suffix or '(无扩展名)'}")

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
            raise HTTPException(status_code=422, detail=f"{original_name} save failed") from exc

        size = file_path.stat().st_size
        if suffix in (".xlsx", ".xls") and size > settings.max_excel_upload_bytes:
            file_path.unlink(missing_ok=True)
            limit_mb = settings.max_excel_upload_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=422,
                detail=f"{original_name} 过大（Excel 上限 {limit_mb}MB），请导出为 CSV 上传")
        # .json 数组形整文件读内存再 json.loads，不可流式 → 超限直接 422（jsonl 真流式不受此限）。
        if suffix == ".json" and size > settings.max_json_upload_bytes:
            file_path.unlink(missing_ok=True)
            limit_mb = settings.max_json_upload_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=422,
                detail=f"{original_name} 过大（JSON 上限 {limit_mb}MB），请改用 JSONL 逐行格式上传")
        if shutil.disk_usage(settings.data_dir).free < size * 2:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="磁盘空间不足，无法导入该文件")

        try:
            units = detect_upload_structure(original_name, file_path)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422, detail=f"{original_name} parse failed: {exc}") from exc

        if not units:                       # Excel 无可用数据 sheet：无占位行则源文件无人回收，直接清掉
            file_path.unlink(missing_ok=True)
            continue

        created = []
        for unit in units:
            ds = Dataset(
                user_id=user.id, name=unit.name, source="upload",
                original_filename=original_name, original_format=unit.original_format,
                file_path=str(file_path), row_count=0,
                columns_json=json.dumps(unit.columns, ensure_ascii=False),
                status="importing", header_row=unit.header_row,
                data_start_row=unit.data_start_row, total_rows_including_header=0,
            )
            session.add(ds)
            created.append((ds, unit))
        await session.commit()              # 短事务：占位行立即可见

        for ds, unit in created:
            ingest_manager.submit(ds.id, source_path=file_path, unit=unit,
                                  version=ds.version, user_id=user.id, session_factory=factory)
            results.append(_out(ds))
            publish(user.id, "dataset", ds.id)
    return results


@router.post("/sample")
async def create_sample_datasets(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # 一键注入内置示例数据集，返回与 /upload 同形的列表（每份均为 ready，可立即 /rows、/export、入图运行）。
    results = []
    for name, rows in SAMPLE_DATASETS:
        ds = await create_dataset(session, user.id, name, rows, source="upload")
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


@router.get("/{ds_id}")
async def get_dataset(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # 摄入状态轮询入口：importing→ready/failed（前端/CLI 据此显示进度/失败）
    return _out(await _get_owned(ds_id, user, session))


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
            max_rows=MAX_ROWS_PER_REQUEST,   # 行数顶：超大范围只读上限行并 truncated=True，防整库进内存
            max_chars=0,                     # 关 agent 字符预算：整页足额，不被 60KB 腰斩
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
        columns=_columns_arg(columns),
        max_rows=MAX_ROWS_PER_REQUEST,       # 行数顶：超大 page_size 只读上限行，防整库进内存
        max_chars=0,                         # 关 agent 字符预算：整页足额，不被 60KB 腰斩且尾行可达
    )
    return {"total": payload["total"], "rows": payload["rows"], "truncated": payload["truncated"]}


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

    path = settings.data_dir / "exports" / f"{uuid4().hex[:8]}_{filename}"
    try:
        ds = await ensure_dataset_materialized(session, ds, settings.data_dir)
        if export_format == "jsonl":
            return StreamingResponse(
                iter_jsonl_lines(session, ds, settings.data_dir),
                media_type="application/x-ndjson",
                headers=_attachment_headers(filename),
            )

        if export_format == "csv":
            return StreamingResponse(           # 真流式：不落临时盘、无首字节延迟、对 1-10G 友好
                iter_csv_lines(session, ds, settings.data_dir),
                media_type="text/csv; charset=utf-8",
                headers=_attachment_headers(filename),
            )

        path = await write_xlsx_export(session, ds, settings.data_dir, path)
        return FileResponse(
            path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(os.unlink, path),
        )
    # 非法单元格/写盘失败 → 清半成品文件后 422，不 500。IllegalCharacterError/OverflowError 非 ValueError 子类，
    # 单元格已在 _xlsx_cell 中和；此处兜底任何未枚举的 openpyxl 非法值，避免逃逸成 500。
    except (OSError, ValueError, IllegalCharacterError, OverflowError) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Export failed") from exc


@router.post("/{ds_id}/versions")
async def create_dataset_version(
    ds_id: int,
    body: dict,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ds = await _get_owned(ds_id, user, session)
    operations = body.get("operations")
    if not isinstance(operations, list):
        raise HTTPException(status_code=422, detail="operations must be a list")
    try:
        new_ds = await apply_dataset_operations(
            session,
            source=ds,
            user_id=user.id,
            data_dir=settings.data_dir,
            operations=operations,
        )
    except DatasetCrudError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    publish(user.id, "dataset", new_ds.id)
    return _out(new_ds)


@router.delete("/{ds_id}")
async def delete_dataset(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ds = await _get_owned(ds_id, user, session)
    # 迁移后真实数据全在分片目录 datasets/<uid>/<id>/v*/，删库行不回收磁盘 → 提交成功后整 id 目录删掉。
    shard_dir = dataset_root(settings.data_dir, ds.user_id, ds.id, ds.version).parent
    file_path = ds.file_path
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds.id))
    await session.delete(ds)
    await session.commit()
    shutil.rmtree(shard_dir, ignore_errors=True)
    if file_path:
        # 多 sheet Excel 共享同一 file_path；只有无其他 Dataset 仍在 importing 时才删源文件，
        # 否则兄弟 sheet 的后台摄入任务会因源文件消失而 failed。
        still = (await session.execute(
            select(Dataset.id).where(
                Dataset.file_path == str(file_path),
                Dataset.status == "importing",
            )
        )).first()
        if still is None:
            Path(file_path).unlink(missing_ok=True)
    publish(user.id, "dataset", ds_id)
    return {"ok": True}
