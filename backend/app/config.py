from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"
    agent_goal_max_rounds: int = 20

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir.as_posix()}/graphflow.db"


settings = Settings()
