import pytest

from app.agent.sandbox import resolve_in


def test_resolve_inside(tmp_path):
    assert resolve_in(tmp_path, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()


def test_resolve_dot_default(tmp_path):
    assert resolve_in(tmp_path, ".") == tmp_path.resolve()


def test_escape_dotdot(tmp_path):
    with pytest.raises(ValueError):
        resolve_in(tmp_path, "../outside.txt")


def test_escape_absolute(tmp_path):
    with pytest.raises(ValueError):
        resolve_in(tmp_path, "C:/Windows/win.ini")
