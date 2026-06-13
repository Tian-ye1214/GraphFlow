import pytest

from app.engine.pycode import run_process_code

ROWS = [{"q": "你好", "n": 1}, {"q": "world", "n": 2}]


async def test_transforms_rows():
    code = "def process(rows):\n    return [{**r, 'q_len': len(r['q'])} for r in rows]"
    out = await run_process_code(code, ROWS)
    assert out == [{"q": "你好", "n": 1, "q_len": 2}, {"q": "world", "n": 2, "q_len": 5}]


async def test_user_print_does_not_corrupt_output():
    code = "def process(rows):\n    print('调试输出')\n    return rows"
    assert await run_process_code(code, ROWS) == ROWS


async def test_empty_code_rejected():
    with pytest.raises(ValueError, match="未生成代码"):
        await run_process_code("  ", ROWS)


async def test_missing_process_fn_fails():
    with pytest.raises(ValueError, match="执行失败"):
        await run_process_code("x = 1", ROWS)


async def test_bad_return_type_fails():
    with pytest.raises(ValueError, match="执行失败"):
        await run_process_code("def process(rows):\n    return 42", ROWS)


async def test_exception_surfaces_traceback():
    with pytest.raises(ValueError, match="boom"):
        await run_process_code("def process(rows):\n    raise RuntimeError('boom')", ROWS)


async def test_timeout_kills(monkeypatch):
    import app.engine.pycode as pc
    monkeypatch.setattr(pc, "CODE_TIMEOUT", 3)
    code = "import time\ndef process(rows):\n    time.sleep(60)\n    return rows"
    with pytest.raises(ValueError, match="超时"):
        await run_process_code(code, ROWS)


async def test_pandas_grouped_dedup_runs_in_subprocess():
    rows = [
        {"session": "s1", "q": "a"}, {"session": "s1", "q": "a"},
        {"session": "s1", "q": "b"}, {"session": "s2", "q": "a"},
    ]
    code = (
        "import pandas as pd\n"
        "def process(rows):\n"
        "    df = pd.DataFrame(rows)\n"
        "    df = df.drop_duplicates(subset=['session', 'q'])\n"
        "    return df.to_dict('records')\n"
    )
    out = await run_process_code(code, rows)
    assert out == [
        {"session": "s1", "q": "a"}, {"session": "s1", "q": "b"}, {"session": "s2", "q": "a"},
    ]
