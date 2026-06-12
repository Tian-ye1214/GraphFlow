from app.agent.extract import extract_text


def test_txt(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n\n\n\nworld", encoding="utf-8")
    assert extract_text(f) == "hello\nworld"


def test_csv(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("q,a\n1,2", encoding="utf-8")
    assert "q,a" in extract_text(f)


def test_missing(tmp_path):
    assert "不存在" in extract_text(tmp_path / "nope.txt")


def test_unsupported(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x00")
    assert "不支持" in extract_text(f)


def test_html(tmp_path):
    f = tmp_path / "a.html"
    f.write_text("<html><script>x()</script><p>正文</p></html>", encoding="utf-8")
    out = extract_text(f)
    assert "正文" in out and "x()" not in out
