"""数据集写入服务单点：删除级联 + 上传摄入收口，REST 与 Agent DatasetToolkit 共用。"""
import json
import shutil
from pathlib import Path

from sqlalchemy import delete as sa_delete, select

from app.events import publish
from app.models import Dataset, DatasetRow
from app.services.dataset_store import dataset_root, detect_upload_structure
from app.services.ingest_manager import ingest_manager


async def delete_dataset(session, ds: Dataset, data_dir) -> None:
    """删除数据集：级联删行 + 提交 + 回收分片目录 + 源文件共享守卫后 unlink + 发布事件。"""
    uid, ds_id, file_path = ds.user_id, ds.id, ds.file_path
    shard_dir = dataset_root(data_dir, ds.user_id, ds.id, ds.version).parent
    await session.execute(sa_delete(DatasetRow).where(DatasetRow.dataset_id == ds_id))
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
    publish(uid, "dataset", ds_id)


async def ingest_file(session, factory, user_id: int, original_filename: str,
                      file_path: Path) -> list[Dataset]:
    """探测文件结构→建占位行(importing)→提交后台摄入。空数据(无 unit)返回 []。
    调用方负责文件已落在可读路径（REST=uploads/，Agent=会话工作目录拷进 uploads/）。"""
    units = detect_upload_structure(original_filename, file_path)
    if not units:
        return []
    created = []
    for unit in units:
        ds = Dataset(
            user_id=user_id, name=unit.name, source="upload",
            original_filename=original_filename, original_format=unit.original_format,
            file_path=str(file_path), row_count=0,
            columns_json=json.dumps(unit.columns, ensure_ascii=False),
            status="importing", header_row=unit.header_row,
            data_start_row=unit.data_start_row, total_rows_including_header=0,
        )
        session.add(ds)
        created.append((ds, unit))
    await session.commit()
    out = []
    for ds, unit in created:
        ingest_manager.submit(ds.id, source_path=file_path, unit=unit,
                              version=ds.version, user_id=user_id, session_factory=factory)
        publish(user_id, "dataset", ds.id)
        out.append(ds)
    return out
