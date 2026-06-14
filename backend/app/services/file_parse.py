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


def parse_file(filename: str, content: bytes) -> list[dict]:
    """把上传文件解析为行式记录。不支持的格式 / 非对象记录抛 ValueError。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in _decode(content).splitlines() if line.strip()]
    elif suffix == ".json":
        data = json.loads(_decode(content))
        rows = data if isinstance(data, list) else [data]
    else:
        if suffix == ".csv":
            # dtype=str：禁用 pandas 类型推断，避免 "007"→7、长 ID→float 丢精度、"true"→bool 等静默篡改；
            # 缺失单元格仍为 NaN→null→None（保持与 JSONL 一致，由模板渲染成空串）。要数值请用 cast 操作显式转。
            df = pd.read_csv(io.StringIO(_decode(content)), dtype=str)
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(content))
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")
        rows = json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))
    if not all(isinstance(r, dict) for r in rows):
        raise ValueError("文件内容须为 JSON 对象（键值对）的列表；标量/数组/null 不能作为数据行")
    return rows


def union_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols
