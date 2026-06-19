import io
import json

import pandas as pd
import pytest

from app.services.file_parse import parse_file, parse_sheets, union_columns


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


def test_corrupt_xlsx_raises_valueerror():
    """损坏 xlsx（zip 魔数但非合法 zip）归一为 ValueError（上传边界→422），不逃逸成 BadZipFile/500。"""
    bad = b"PK\x03\x04 not really a zip"
    with pytest.raises(ValueError):
        parse_file("x.xlsx", bad)
    with pytest.raises(ValueError):
        parse_sheets("x.xlsx", bad)


def test_union_columns_keeps_order():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert union_columns(rows) == ["a", "b", "c"]


def test_csv_numeric_json_serializable():
    rows = parse_file("a.csv", b"n,price\n1,3.5\n2,")
    for row in rows:
        json.dumps(row, ensure_ascii=False)


def test_csv_preserves_strings_no_type_coercion():
    """CSV 不再静默推断类型：前导零/超长 ID/布尔字面量 一律按字符串保真（与 JSONL 一致）。"""
    rows = parse_file("a.csv", b"phone,flag,big\n007,true,12345678901234567890\n")
    assert rows == [{"phone": "007", "flag": "true", "big": "12345678901234567890"}]


def test_non_object_records_rejected():
    """标量/null/数组/混入裸值的 JSON/JSONL 应抛 ValueError（上传路径转 422）。"""
    for content in (b"42", b"null", b"[1, 2, 3]", b'"hi"'):
        with pytest.raises(ValueError, match="JSON 对象"):
            parse_file("x.json", content)
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_file("x.jsonl", b'{"q": 1}\n99\n')


def test_csv_preserves_na_like_literals():
    """CSV 字面量 None/NA/null 不被默认 NA 列表当缺失吞掉（keep_default_na=False）。"""
    rows = parse_file("a.csv", b"x,y\nNone,NA\nnull,foo\n")
    assert rows == [{"x": "None", "y": "NA"}, {"x": "null", "y": "foo"}]


def test_xlsx_preserves_text_no_coercion():
    """Excel 文本单元格不被 read_excel 推断篡改：前导零/长ID/布尔字面量/字面量None 保字符串。"""
    buf = io.BytesIO()
    pd.DataFrame([{"id": "007", "flag": "false", "big": "12345678901234567890", "na": "None"}]
                 ).to_excel(buf, index=False)
    rows = parse_file("a.xlsx", buf.getvalue())
    assert rows == [{"id": "007", "flag": "false", "big": "12345678901234567890", "na": "None"}]


def test_parse_sheets_multi_and_single():
    """多 sheet → 每非空 sheet 一个 (stem-sheet名, 行)，空 sheet 跳过；单 sheet 用 stem 名。"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        pd.DataFrame([{"q": "甲"}]).to_excel(w, sheet_name="S1", index=False)
        pd.DataFrame([{"k": "v", "n": "x"}]).to_excel(w, sheet_name="S2", index=False)
        pd.DataFrame().to_excel(w, sheet_name="空", index=False)
    out = parse_sheets("书.xlsx", buf.getvalue())
    assert [name for name, _ in out] == ["书-S1", "书-S2"]      # 空 sheet 跳过
    assert dict(out)["书-S1"] == [{"q": "甲"}] and dict(out)["书-S2"] == [{"k": "v", "n": "x"}]

    buf2 = io.BytesIO()
    pd.DataFrame([{"q": "x"}]).to_excel(buf2, sheet_name="OnlyOne", index=False)
    assert [name for name, _ in parse_sheets("单.xlsx", buf2.getvalue())] == ["单"]  # 单 sheet 用 stem


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
