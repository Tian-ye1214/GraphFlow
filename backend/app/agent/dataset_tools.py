"""Agent 数据集写工具：上传/导出走会话工作目录沙箱(resolve_in)，复用 dataset_service 单点。"""
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.sandbox import resolve_in
from app.config import settings
from app.models import Dataset
from app.services import dataset_service
from app.services.dataset_store import iter_jsonl_lines, iter_csv_lines, write_xlsx_export


class DatasetToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int, workdir,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._workdir = Path(workdir)
        self._confirm_delete = confirm_delete

    async def _owned(self, s, ds_id: int):
        ds = await s.get(Dataset, int(ds_id))
        return ds if ds is not None and ds.user_id == self._uid else None

    async def upload_dataset(self, file_path: str, name: str | None = None) -> str:
        """把会话工作目录里的文件(jsonl/json/csv/xlsx/xls)上传为数据集(异步摄入)。
        Parameters:
            file_path: 文件路径，相对会话工作目录(先用 write_file 落文件)
            name: 可选数据集名(留空用文件名)
        """
        try:
            src = resolve_in(self._workdir, file_path)
        except ValueError as e:
            return f"Security error: {e}"
        if not src.exists():
            return f"Error: 文件不存在 {file_path}"
        # 拷进 uploads/ 作摄入源(摄入会回收源文件，勿直接吃掉用户工作目录文件)
        upload_dir = settings.data_dir / "uploads" / str(self._uid)
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{uuid4().hex[:8]}_{src.name}"
        shutil.copy2(src, dest)
        original = name or src.name
        async with self._sf() as s:
            try:
                created = await dataset_service.ingest_file(s, self._sf, self._uid,
                                                            original, dest)
            except (ValueError, UnicodeDecodeError, RecursionError) as e:
                dest.unlink(missing_ok=True)
                return f"Error: 解析失败 {e}"
        if not created:
            dest.unlink(missing_ok=True)
            return "Error: 文件无可用数据"
        return "已上传摄入(后台进行中)：" + ", ".join(f"{d.name}(#{d.id})" for d in created)

    async def export_dataset(self, dataset_id: int, format: str = "jsonl") -> str:
        """把数据集导出到会话工作目录。format=jsonl/csv/xlsx。
        Parameters:
            dataset_id: 数据集 id
            format: jsonl/csv/xlsx
        """
        fmt = format.lower()
        if fmt not in ("jsonl", "csv", "xlsx"):
            return "Error: format 须为 jsonl/csv/xlsx"
        async with self._sf() as s:
            ds = await self._owned(s, dataset_id)
            if ds is None:
                return "数据集不存在"
            out_path = resolve_in(self._workdir, f"dataset_{dataset_id}.{fmt}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "xlsx":
                await write_xlsx_export(s, ds, settings.data_dir, out_path)
            elif fmt == "jsonl":
                with out_path.open("w", encoding="utf-8") as fh:
                    async for line in iter_jsonl_lines(s, ds, settings.data_dir):
                        fh.write(line)  # line 已含 \n
            else:  # csv
                with out_path.open("w", encoding="utf-8", newline="") as fh:
                    async for chunk in iter_csv_lines(s, ds, settings.data_dir):
                        fh.write(chunk)  # chunk 已含 \n
        return f"已导出到工作目录 dataset_{dataset_id}.{fmt}"

    async def delete_dataset(self, dataset_id: int) -> str:
        """删除数据集(级联删行/磁盘分片/源文件)。需用户确认。
        Parameters:
            dataset_id: 数据集 id
        """
        async with self._sf() as s:
            ds = await self._owned(s, dataset_id)
            if ds is None:
                return "数据集不存在"
            if not self._confirm_delete:
                return ("删除数据集需用户确认：请向用户说明将删除数据集及其全部行与磁盘文件，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf data rm {dataset_id}，然后结束回合等待确认。")
            await dataset_service.delete_dataset(s, ds, settings.data_dir)
        return f"已删除数据集 #{dataset_id}"

    @property
    def tools(self) -> list:
        return [self.upload_dataset, self.export_dataset, self.delete_dataset]
