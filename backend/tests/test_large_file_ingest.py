"""Spec 1（大文件统一摄入/导出）回归测试。
Task 1: 基础设施（紧凑序列化 / import_error 列）。后续任务陆续追加。
"""
import datetime
import json

from openpyxl import Workbook

from app.models import Dataset, User
from app.services.dataset_store import _dumps_row


def _xlsx(path, sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    wb.save(path)


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


# --- Task 2: 结构探测 / 行迭代 / 批量写分片 -------------------------------

def test_detect_csv_structure(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("q,a\n1,2\n", encoding="utf-8")
    from app.services.dataset_store import detect_upload_structure
    units = detect_upload_structure("d.csv", p)
    assert len(units) == 1
    u = units[0]
    assert u.columns == ["q", "a"] and u.header_row == 1 and u.data_start_row == 2
    assert u.reader == "csv" and u.original_format == "csv" and u.name == "d"


def test_detect_jsonl_structure(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"a":1}\n', encoding="utf-8")
    from app.services.dataset_store import detect_upload_structure
    units = detect_upload_structure("d.jsonl", p)
    assert len(units) == 1 and units[0].columns == [] and units[0].reader == "jsonl"
    assert units[0].header_row is None and units[0].data_start_row == 1


def test_detect_json_uses_json_reader(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('[{"a":1}]', encoding="utf-8")
    from app.services.dataset_store import detect_upload_structure
    u = detect_upload_structure("d.json", p)[0]
    assert u.reader == "json" and u.original_format == "jsonl"


def test_detect_multisheet_xlsx(tmp_path):
    p = tmp_path / "book.xlsx"
    _xlsx(p, [("alpha", [["q", "a"], ["1", "2"]]), ("beta", [["x"], ["9"]])])
    from app.services.dataset_store import detect_upload_structure
    units = detect_upload_structure("book.xlsx", p)
    assert [u.name for u in units] == ["book-alpha", "book-beta"]
    assert units[0].columns == ["q", "a"] and units[0].sheet_index == 0
    assert units[1].columns == ["x"] and units[1].sheet_index == 1


def test_detect_xlsx_skips_dataless_sheet(tmp_path):
    p = tmp_path / "b.xlsx"
    _xlsx(p, [("hasdata", [["c"], ["v"]]), ("headeronly", [["h"]])])
    from app.services.dataset_store import detect_upload_structure
    units = detect_upload_structure("b.xlsx", p)
    assert len(units) == 1 and units[0].name == "b" and units[0].sheet_index == 0


def test_rows_for_unit_csv(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("q,a\n1,2\n3,4\n", encoding="utf-8")
    from app.services.dataset_store import detect_upload_structure, rows_for_unit
    u = detect_upload_structure("d.csv", p)[0]
    assert list(rows_for_unit(u, p)) == [{"q": "1", "a": "2"}, {"q": "3", "a": "4"}]


def test_rows_for_unit_xlsx_sheet(tmp_path):
    p = tmp_path / "b.xlsx"
    _xlsx(p, [("s1", [["q", "a"], ["1", "2"]]), ("s2", [["x"], ["9"]])])
    from app.services.dataset_store import detect_upload_structure, rows_for_unit
    units = detect_upload_structure("b.xlsx", p)
    assert list(rows_for_unit(units[1], p)) == [{"x": "9"}]


def test_parse_and_write_shards_with_progress(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("q\n" + "\n".join(str(i) for i in range(5)) + "\n", encoding="utf-8")
    from app.services.dataset_store import detect_upload_structure, parse_and_write_shards
    u = detect_upload_structure("d.csv", p)[0]
    seen = []
    manifest, cols, n = parse_and_write_shards(
        source_path=p, unit=u, data_dir=tmp_path / "dd",
        user_id=1, dataset_id=1, version=1, shard_size=2, progress_cb=seen.append)
    assert n == 5 and cols == ["q"]
    assert len(manifest["shards"]) == 3          # 2+2+1
    assert seen == [2, 4, 5]                      # 每分片关闭回调一次累计行数
