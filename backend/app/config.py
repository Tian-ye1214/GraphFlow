from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"
    agent_goal_max_rounds: int = 20
    goal_no_improve_k: int = 2  # 目标模式：连续无提升轮数早停阈值
    admin_users: str = ""  # 逗号分隔的管理员用户名白名单

    @property
    def admin_user_set(self) -> set[str]:
        return {u.strip() for u in self.admin_users.split(",") if u.strip()}

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir.as_posix()}/graphflow.db"


settings = Settings()
