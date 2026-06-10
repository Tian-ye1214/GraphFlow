import io
import json

import pandas as pd
import pytest

from app.services.file_parse import parse_file, union_columns


def test_jsonl():
    content = '{"q": "你好", "a": "world"}\n\n{"q": "第二行"}\n'.encode("utf-8")
    rows = parse_file("a.jsonl", content)
    assert rows == [{"q": "你好", "a": "world"}, {"q": "第二行"}]


def test_json_array_and_single():
    assert parse_file("a.json", b'[{"x": 1}, {"x": 2}]') == [{"x": 1}, {"x": 2}]
    assert parse_file("a.json", '{"x": "单条"}'.encode()) == [{"x": "单条"}]


def test_csv():
    rows = parse_file("a.csv", "q,a\n你好,world\n".encode("utf-8"))
    assert rows == [{"q": "你好", "a": "world"}]


def test_xlsx():
    buf = io.BytesIO()
    pd.DataFrame([{"q": "你好", "a": 1}]).to_excel(buf, index=False)
    rows = parse_file("a.xlsx", buf.getvalue())
    assert rows == [{"q": "你好", "a": 1}]


def test_unsupported_suffix():
    with pytest.raises(ValueError, match="不支持"):
        parse_file("a.txt", b"hello")


def test_union_columns_keeps_order():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert union_columns(rows) == ["a", "b", "c"]


def test_csv_numeric_json_serializable():
    rows = parse_file("a.csv", b"n,price\n1,3.5\n2,")
    for row in rows:
        json.dumps(row, ensure_ascii=False)


def test_xlsx_numeric_json_serializable():
    buf = io.BytesIO()
    pd.DataFrame([{"n": 1, "x": 2.5}]).to_excel(buf, index=False)
    rows = parse_file("a.xlsx", buf.getvalue())
    for row in rows:
        json.dumps(row, ensure_ascii=False)


def test_xlsx_datetime_becomes_iso_string():
    buf = io.BytesIO()
    pd.DataFrame([{"name": "Alice", "ts": pd.Timestamp("2024-01-15")}]).to_excel(buf, index=False)
    rows = parse_file("a.xlsx", buf.getvalue())
    assert isinstance(rows[0]["ts"], str) and rows[0]["ts"].startswith("2024-01-15")
    for row in rows:
        json.dumps(row, ensure_ascii=False)
