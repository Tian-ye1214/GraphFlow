import pytest

from app.engine.nodes import apply_operations

ROWS = [{"q": "你好", "n": "1"}, {"q": "你好", "n": "2"}, {"q": "world", "n": "3"}]


def test_dedup_by_columns():
    out = apply_operations(ROWS, [{"op": "dedup", "columns": ["q"]}])
    assert [r["n"] for r in out] == ["1", "3"]  # 保留首次出现


def test_dedup_all_columns_default():
    rows = [{"a": 1}, {"a": 1}, {"a": 2}]
    assert apply_operations(rows, [{"op": "dedup"}]) == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize("mode,value,expected_n", [
    ("min_len", 3, ["3"]),          # len("world")=5 >= 3
    ("max_len", 2, ["1", "2"]),     # len("你好")=2
    ("contains", "world", ["3"]),
    ("not_contains", "world", ["1", "2"]),
    ("regex", "^你", ["1", "2"]),
])
def test_filter_modes(mode, value, expected_n):
    out = apply_operations(ROWS, [{"op": "filter", "column": "q", "mode": mode, "value": value}])
    assert [r["n"] for r in out] == expected_n


def test_rename_drop_concat():
    out = apply_operations(ROWS[:1], [
        {"op": "rename", "mapping": {"q": "question"}},
        {"op": "concat", "target": "merged", "columns": ["question", "n"], "sep": "-"},
        {"op": "drop", "columns": ["n"]},
    ])
    assert out == [{"question": "你好", "merged": "你好-1"}]


def test_cast():
    out = apply_operations([{"x": "3"}], [{"op": "cast", "column": "x", "to": "int"}])
    assert out == [{"x": 3}]
    with pytest.raises(ValueError):
        apply_operations([{"x": "abc"}], [{"op": "cast", "column": "x", "to": "int"}])


def test_sample_and_shuffle_deterministic_with_seed():
    rows = [{"i": i} for i in range(10)]
    a = apply_operations(rows, [{"op": "sample", "n": 5}], seed=42)
    b = apply_operations(rows, [{"op": "sample", "n": 5}], seed=42)
    assert a == b and len(a) == 5
    c = apply_operations(rows, [{"op": "shuffle"}], seed=42)
    assert sorted(r["i"] for r in c) == list(range(10))


def test_sample_larger_than_rows_returns_all():
    rows = [{"i": 1}]
    assert apply_operations(rows, [{"op": "sample", "n": 99}]) == rows


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="未知操作"):
        apply_operations(ROWS, [{"op": "magic"}])
