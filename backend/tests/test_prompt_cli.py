from test_cli import gf, server  # noqa: F401  复用 server fixture（pytest 按名注入，看似未用实为必需）


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


def _wf_with_node(server):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流"); gf("use", "流"); gf("node", "add", "llm", "n1")


def test_node_prompt_library_ref(server, capsys, tmp_path):
    _wf_with_node(server)
    f = tmp_path / "p.md"; f.write_text("模板 {{q}}", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f))
    capsys.readouterr()
    gf("node", "prompt", "n1", "--system", "--library", "P", "--ref")
    assert "引用" in capsys.readouterr().out
    capsys.readouterr()
    gf("node", "show", "n1")
    assert "system_prompt_ref" in capsys.readouterr().out


def test_node_prompt_library_copy(server, capsys, tmp_path):
    _wf_with_node(server)
    f = tmp_path / "p.md"; f.write_text("正文内容 {{q}}", encoding="utf-8")
    gf("prompt", "add", "P", "--file", str(f))
    capsys.readouterr()
    gf("node", "prompt", "n1", "--user", "--library", "P", "--copy")
    assert "复制" in capsys.readouterr().out
    capsys.readouterr()
    gf("node", "show", "n1")
    shown = capsys.readouterr().out
    assert "正文内容" in shown and "user_prompt_ref" not in shown
