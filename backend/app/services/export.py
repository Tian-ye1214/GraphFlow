import csv
import io
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from openpyxl import Workbook

from app.engine.columns import ordered_union
from app.services.dataset_store import _jsonify_nested

CSV_FLUSH_BYTES = 64 * 1024


def iter_jsonl(rows: Iterable[dict]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(row, ensure_ascii=False) + "\n"


def iter_csv(rows: Iterable[dict], columns: list[str]) -> Iterator[str]:
    """边写边 ~64KB 一批 yield：不落临时盘、不经 pandas 整表副本。嵌套单元格串 JSON 文本与数据集导出一致。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_jsonify_nested(row.get(col, "")) for col in columns])
        if buf.tell() >= CSV_FLUSH_BYTES:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
    if buf.tell():
        yield buf.getvalue()


async def aiter_jsonl(rows: Iterable[dict]):
    """流式响应版：原生异步生成器。StreamingResponse 喂同步生成器要走线程池迭代，高负载下偶发空体；
    异步生成器是 Starlette 的原生路径（与数据集导出 iter_jsonl_lines 一致），可靠。"""
    for chunk in iter_jsonl(rows):
        yield chunk


async def aiter_csv(rows: Iterable[dict], columns: list[str]):
    for chunk in iter_csv(rows, columns):
        yield chunk


def write_xlsx(rows: Iterable[dict], columns: list[str], path: Path) -> Path:
    """write_only 工作簿逐行 append：内存有界，不经 pandas DataFrame 整表副本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("data")
    ws.append(columns)
    for row in rows:
        ws.append([_jsonify_nested(row.get(col, "")) for col in columns])
    wb.save(path)
    return path


def export_rows(rows: list[dict], fmt: str, path: Path) -> Path:
    """落盘版导出（dev 工具沿用）：内部走与流式响应同一套 helper，不再经 pandas。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(iter_jsonl(rows))
    elif fmt == "csv":
        columns = ordered_union([list(row) for row in rows])
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            for chunk in iter_csv(rows, columns):
                fh.write(chunk)
    elif fmt == "xlsx":
        write_xlsx(rows, ordered_union([list(row) for row in rows]), path)
    else:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return path
