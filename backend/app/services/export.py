import json
from pathlib import Path

import pandas as pd


def export_rows(rows: list[dict], fmt: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                        encoding="utf-8")
    elif fmt == "csv":
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    elif fmt == "xlsx":
        pd.DataFrame(rows).to_excel(path, index=False)
    else:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return path
