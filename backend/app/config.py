from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'graphflow.db'}"


settings = Settings()
