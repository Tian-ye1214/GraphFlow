import importlib
from pathlib import Path


def test_state_file_env_override(monkeypatch, tmp_path):
    from app import cli
    p = tmp_path / "s.json"
    monkeypatch.setenv("GF_STATE_FILE", str(p))
    importlib.reload(cli)
    assert cli.STATE_FILE == p
    monkeypatch.delenv("GF_STATE_FILE")
    importlib.reload(cli)
    assert cli.STATE_FILE == Path.home() / ".graphflow" / "cli.json"
