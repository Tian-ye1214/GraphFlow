import io
import json
from pathlib import Path

import pandas as pd


def _decode(content: bytes) -> str:
    """容错解码文本类文件：优先 UTF-8（连带剥除 BOM），失败回退 GBK（Windows 中文 Excel/记事本常见）。"""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("gbk")


def _records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))


def parse_file(filename: str, content: bytes) -> list[dict]:
    """把上传文件解析为行式记录（Excel 取首个 sheet）。不支持的格式 / 非对象记录抛 ValueError。
    上传走 parse_sheets（多 sheet），此函数供非 Excel 路径与直接调用。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in _decode(content).splitlines() if line.strip()]
    elif suffix == ".json":
        data = json.loads(_decode(content))
        rows = data if isinstance(data, list) else [data]
    elif suffix == ".csv":
        # dtype=str 禁类型推断（"007"→7、长 ID→float 丢精度、"true"→bool）；
        # keep_default_na=False 保留 "None"/"NA"/"null" 等字面量（不被默认 NA 列表当缺失吞掉）。
        rows = _records(pd.read_csv(io.StringIO(_decode(content)), dtype=str, keep_default_na=False))
    elif suffix in (".xlsx", ".xls"):
        rows = _records(_read_excel(io.BytesIO(content)))
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")
    if not all(isinstance(r, dict) for r in rows):
        raise ValueError("文件内容须为 JSON 对象（键值对）的列表；标量/数组/null 不能作为数据行")
    return rows


def _read_excel(buf, sheet_name=0):
    # dtype=object 保单元格存储类型（文本保文本"007"/真数值保数值/日期保日期），不像 dtype=str 把数字也变串；
    # keep_default_na=False 保 "None"/"NA" 字面量。配合 read_excel 默认推断会把文本 "007"→7、"false"→bool。
    return pd.read_excel(buf, sheet_name=sheet_name, dtype=object, keep_default_na=False)


def parse_sheets(filename: str, content: bytes) -> list[tuple[str, list[dict]]]:
    """Excel 全部 sheet → 每个非空 sheet 一个 (数据集名, 行)。
    单 sheet 用文件名 stem 命名；多 sheet 用 stem-sheet名；空 sheet 跳过。"""
    stem = Path(filename).stem
    sheets = _read_excel(io.BytesIO(content), sheet_name=None)  # {sheet名: df}，保 sheet 顺序
    items = [(name, _records(df)) for name, df in sheets.items()]
    items = [(name, rows) for name, rows in items if rows]      # 跳过空 sheet
    return [(stem if len(items) == 1 else f"{stem}-{name}", rows) for name, rows in items]


def union_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols
