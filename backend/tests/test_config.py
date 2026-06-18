from pathlib import Path


def test_default_settings():
    from app.config import Settings
    s = Settings()
    assert s.data_dir == Path("data")
    assert s.db_url.startswith("sqlite+aiosqlite:///")


def test_env_override(monkeypatch):
    monkeypatch.setenv("GRAPHFLOW_DATA_DIR", "/tmp/gf")
    monkeypatch.setenv("GRAPHFLOW_SECRET_KEY", "s3cret")
    from app.config import Settings
    s = Settings()
    assert s.data_dir == Path("/tmp/gf")
    assert s.secret_key == "s3cret"
