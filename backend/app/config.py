from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"
    agent_goal_max_rounds: int = 20
    goal_no_improve_k: int = 2  # 目标模式：连续无提升轮数早停阈值
    admin_users: str = ""  # 逗号分隔的管理员用户名白名单
    # 大文件摄入护栏：openpyxl 解析超大 xlsx 必 OOM，源 xlsx/xls 超此字节直接 422（引导转 CSV）；
    # CSV/JSONL 走真流式无此限。后台摄入全局并发上限，避免多个大文件解析互相踩+撑爆线程池。
    max_excel_upload_bytes: int = 200 * 1024 * 1024
    # .json（数组形）整文件读进内存再 json.loads（不可流式），超此字节直接 422（引导转 JSONL 逐行）。
    max_json_upload_bytes: int = 200 * 1024 * 1024
    ingest_concurrency: int = 2

    @property
    def admin_user_set(self) -> set[str]:
        return {u.strip() for u in self.admin_users.split(",") if u.strip()}

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir.as_posix()}/graphflow.db"


settings = Settings()
