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
    """把上传文件解析为行式记录。不支持的格式抛 ValueError。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".jsonl":
        return [json.loads(line) for line in _decode(content).splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(_decode(content))
        return data if isinstance(data, list) else [data]
    if suffix == ".csv":
        df = pd.read_csv(io.StringIO(_decode(content)))
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(content))
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")
    return json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))


def union_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols
