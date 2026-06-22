"""后台异步数据集摄入：上传端点同步建占位行后，把「逐行写分片」这步交给本管理器在
事件循环外(asyncio.to_thread)执行，避免大文件解析卡死整个服务，也避免上传请求被代理超时。
所有尺寸走同一条路径——小文件只是瞬间 importing→ready，不区分大/小文件。
"""
import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.events import publish
from app.models import Dataset
from app.services.dataset_store import (ParseUnit, dataset_root, dump_manifest,
                                        parse_and_write_shards, visible_total)


def _progress_writer(dataset_id: int):
    """进度回调在 to_thread 工作线程内执行：用 stdlib sqlite3 短写 imported_rows，
    不跨线程碰 async session；每分片(10万行)一次，WAL 下极轻。"""
    db_path = str(settings.data_dir / "graphflow.db")

    def cb(row_count: int) -> None:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("UPDATE datasets SET imported_rows=? WHERE id=?", (row_count, dataset_id))
            conn.commit()
        finally:
            conn.close()

    return cb


class IngestManager:
    """摄入任务的进程内登记处：全局并发上限 + 后台 asyncio.Task。"""

    def __init__(self):
        self._sem: tuple[int, asyncio.Semaphore] | None = None
        self._running: dict[int, asyncio.Task] = {}

    def _semaphore(self) -> asyncio.Semaphore:
        # 连同容量缓存：ingest_concurrency 变更下次提交即生效（同 RunManager.user_sem 范式）
        cap = settings.ingest_concurrency
        if self._sem is None or self._sem[0] != cap:
            self._sem = (cap, asyncio.Semaphore(cap))
        return self._sem[1]

    def submit(self, dataset_id: int, *, source_path: Path, unit: ParseUnit, version: int,
               user_id: int, session_factory: async_sessionmaker) -> None:
        task = asyncio.create_task(
            self._run_ingest(dataset_id, source_path, unit, version, user_id, session_factory))
        self._running[dataset_id] = task
        task.add_done_callback(lambda _: self._running.pop(dataset_id, None))

    async def _run_ingest(self, dataset_id, source_path, unit, version, user_id, session_factory):
        async with self._semaphore():
            try:
                manifest, columns, row_count = await asyncio.to_thread(
                    parse_and_write_shards,
                    source_path=source_path, unit=unit, data_dir=settings.data_dir,
                    user_id=user_id, dataset_id=dataset_id, version=version,
                    progress_cb=_progress_writer(dataset_id))
            except Exception as exc:  # noqa: BLE001 深层解析错统一落 failed，不让后台任务静默崩
                await self._finalize_failed(dataset_id, user_id, str(exc), session_factory)
            else:
                await self._finalize_ready(
                    dataset_id, user_id, manifest, columns, row_count, unit, session_factory)
            finally:
                await self._reclaim_source_if_done(source_path, session_factory)

    async def _finalize_ready(self, dataset_id, user_id, manifest, columns, row_count, unit,
                              session_factory):
        async with session_factory() as s:
            ds = await s.get(Dataset, dataset_id)
            if ds is None:
                return
            ds.manifest_json = dump_manifest(manifest)
            ds.columns_json = json.dumps(columns, ensure_ascii=False)
            ds.row_count = row_count
            ds.imported_rows = row_count
            ds.total_rows_including_header = visible_total(row_count, unit.header_row)
            ds.status = "ready"
            ds.import_error = ""
            await s.commit()
        publish(user_id, "dataset", dataset_id)

    async def _finalize_failed(self, dataset_id, user_id, msg, session_factory):
        async with session_factory() as s:
            ds = await s.get(Dataset, dataset_id)
            if ds is None:
                return
            shutil.rmtree(dataset_root(settings.data_dir, ds.user_id, ds.id, ds.version).parent,
                          ignore_errors=True)  # 清孤儿分片
            ds.status = "failed"
            ds.import_error = msg[:2000]
            ds.row_count = 0
            ds.imported_rows = 0
            await s.commit()
        publish(user_id, "dataset", dataset_id)

    async def _reclaim_source_if_done(self, source_path, session_factory):
        # 分片是 canonical 唯一读源，源文件解析后无用；多 sheet 共享同一源，待全部离开 importing 再删。
        async with session_factory() as s:
            still = (await s.execute(
                select(Dataset.id).where(Dataset.file_path == str(source_path),
                                         Dataset.status == "importing"))).first()
        if still is None:
            Path(source_path).unlink(missing_ok=True)


ingest_manager = IngestManager()


async def resume_unfinished(session_factory: async_sessionmaker) -> int:
    """进程启动时把残留 importing(上次崩溃/重启中断)标 failed + 清孤儿分片 + 删源。不自动重解析。"""
    async with session_factory() as s:
        rows = (await s.execute(
            select(Dataset).where(Dataset.status == "importing"))).scalars().all()
        for ds in rows:
            shutil.rmtree(dataset_root(settings.data_dir, ds.user_id, ds.id, ds.version).parent,
                          ignore_errors=True)
            ds.status = "failed"
            ds.import_error = "服务重启，导入中断，请重新上传"
            ds.row_count = 0
            ds.imported_rows = 0
            if ds.file_path:
                Path(ds.file_path).unlink(missing_ok=True)
        await s.commit()
    return len(rows)
