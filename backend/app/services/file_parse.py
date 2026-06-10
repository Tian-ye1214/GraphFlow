import io
import json
from pathlib import Path

import pandas as pd


def parse_file(filename: str, content: bytes) -> list[dict]:
    """把上传文件解析为行式记录。不支持的格式抛 ValueError。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".jsonl":
        return [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(content)
        return data if isinstance(data, list) else [data]
    if suffix == ".csv":
        df = pd.read_csv(io.BytesIO(content))
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
