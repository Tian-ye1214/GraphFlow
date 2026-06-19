"""gf node 命令健壮性（纯函数级，不起服务器）。"""
from types import SimpleNamespace

import pytest


def _prompt_args(**kw):
    base = dict(file=None, edit=False, from_stdin=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_read_prompt_missing_file_dies(tmp_path):
    """node prompt --file 指向不存在文件 → 优雅 die(SystemExit)，不裸 FileNotFoundError（对齐 data up）。"""
    from app.cli.commands.node import _read_prompt
    with pytest.raises(SystemExit):
        _read_prompt(_prompt_args(file=str(tmp_path / "nope.md")))


def test_read_prompt_file_ok(tmp_path):
    """回归：--file 指向存在文件正常读取。"""
    from app.cli.commands.node import _read_prompt
    p = tmp_path / "p.md"; p.write_text("内容", encoding="utf-8")
    assert _read_prompt(_prompt_args(file=str(p))) == "内容"


def test_read_prompt_edit_missing_editor_dies(monkeypatch):
    """node prompt --edit 且 EDITOR 指向不存在程序 → 优雅 die(SystemExit)，不裸 FileNotFoundError。"""
    from app.cli.commands.node import _read_prompt
    monkeypatch.setenv("EDITOR", "definitely_not_a_real_editor_xyz_123")
    with pytest.raises(SystemExit):
        _read_prompt(_prompt_args(edit=True))


def test_parse_colon_map_missing_colon_dies():
    """extract/headers 含不带冒号的段 → die，不把用户输入静默吞成空 dict。"""
    from app.cli.commands.node import _parse_colon_map
    with pytest.raises(SystemExit):
        _parse_colon_map("abc", "extract", "列:JSON路径")
    with pytest.raises(SystemExit):
        _parse_colon_map("a:1,bad", "extract", "列:JSON路径")


def test_parse_colon_map_ok():
    """回归：正常解析；值可含冒号(只切首个)；跳过尾随空段。"""
    from app.cli.commands.node import _parse_colon_map
    assert _parse_colon_map("a:x,b:y", "extract", "列:JSON路径") == {"a": "x", "b": "y"}
    assert _parse_colon_map("Authorization:Bearer abc,", "headers", "名:值") == {"Authorization": "Bearer abc"}
