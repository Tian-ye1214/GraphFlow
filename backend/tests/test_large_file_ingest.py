"""Spec 1（大文件统一摄入/导出）回归测试。
Task 1: 基础设施（紧凑序列化 / import_error 列）。后续任务陆续追加。
"""
import datetime
import json

from app.models import Dataset, User
from app.services.dataset_store import _dumps_row


# --- Task 1: _dumps_row 紧凑分隔符 -----------------------------------------

def test_dumps_row_uses_compact_separators():
    assert _dumps_row({"a": 1, "b": "x"}) == '{"a":1,"b":"x"}'   # 无逗号/冒号后空格


def test_dumps_row_compact_still_neutralizes_and_serializes():
    out = _dumps_row({"x": float("inf"), "dt": datetime.datetime(2024, 1, 2, 3, 4)})
    parsed = json.loads(out)
    assert parsed["x"] is None and "2024-01-02" in parsed["dt"]
    assert ", " not in out and ": " not in out


# --- Task 1: Dataset.import_error 列 ----------------------------------------

async def test_dataset_has_import_error_column(session_factory):
    async with session_factory() as s:
        u = User(username="ingest_col_user", display_name="x")
        s.add(u)
        await s.commit()
        ds = Dataset(user_id=u.id, name="d", status="failed", import_error="boom")
        s.add(ds)
        await s.commit()
        got = await s.get(Dataset, ds.id)
    assert got.import_error == "boom"
