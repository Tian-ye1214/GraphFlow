import app.cli as cli
from test_cli import gf, server   # 复用 server fixture 与 gf 包装


def _login(server):
    gf("login", "tester", "--server", server)


def test_prompt_add_and_ls(server, capsys, tmp_path):
    _login(server)
    f = tmp_path / "p.md"
    f.write_text("你好 {{name}}", encoding="utf-8")
    gf("prompt", "add", "问候", "--file", str(f), "--desc", "打招呼")
    capsys.readouterr()
    gf("prompt", "ls")
    assert "问候" in capsys.readouterr().out


def test_prompt_edit_creates_version(server, capsys, tmp_path):
    _login(server)
    f1 = tmp_path / "a.md"; f1.write_text("v1", encoding="utf-8")
    f2 = tmp_path / "b.md"; f2.write_text("v2", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f1))
    gf("prompt", "edit", "P", "--file", str(f2))
    capsys.readouterr()
    gf("prompt", "versions", "P")
    out = capsys.readouterr().out
    assert "v1" in out and "2" in out


def test_prompt_rollback_and_dup_and_rm(server, capsys, tmp_path):
    _login(server)
    f1 = tmp_path / "a.md"; f1.write_text("一", encoding="utf-8")
    f2 = tmp_path / "b.md"; f2.write_text("二", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f1))
    gf("prompt", "edit", "P", "--file", str(f2))
    gf("prompt", "rollback", "P", "1")
    gf("prompt", "dup", "P", "--name", "P2")
    capsys.readouterr()
    gf("prompt", "ls")
    out = capsys.readouterr().out
    assert "P" in out and "P2" in out
    gf("prompt", "rm", "P2")
    capsys.readouterr()
    gf("prompt", "ls")
    assert "P2" not in capsys.readouterr().out
