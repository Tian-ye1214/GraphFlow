from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str] = mapped_column(default="")
    auth_provider: Mapped[str] = mapped_column(default="dev")
    max_llm_concurrency: Mapped[int] = mapped_column(default=8)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    model_name: Mapped[str] = mapped_column(default="")  # 实际请求用的模型 ID，如 qwen-max
    base_url: Mapped[str]
    api_key_enc: Mapped[str]
    default_params_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    source: Mapped[str] = mapped_column(default="upload")  # upload / run
    original_filename: Mapped[str] = mapped_column(default="")
    file_path: Mapped[str] = mapped_column(default="")
    row_count: Mapped[int] = mapped_column(default=0)
    columns_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DatasetRow(Base):
    __tablename__ = "dataset_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    idx: Mapped[int]
    data_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_dataset_row_unit", "dataset_id", "idx", unique=True),)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    graph_json: Mapped[str] = mapped_column(Text, default='{"nodes": [], "edges": []}')
    is_template: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    version: Mapped[int]
    graph_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    workflow_version_id: Mapped[int] = mapped_column(ForeignKey("workflow_versions.id"))
    status: Mapped[str] = mapped_column(default="queued")  # queued/running/cancelled/completed/failed
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class RunNodeState(Base):
    __tablename__ = "run_node_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    node_id: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending/running/done/failed
    total: Mapped[int] = mapped_column(default=0)
    done: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (Index("ix_node_state_unit", "run_id", "node_id", unique=True),)


class RunRow(Base):
    __tablename__ = "run_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    node_id: Mapped[str]
    row_idx: Mapped[int]
    attempt: Mapped[int] = mapped_column(default=0)
    qc_round: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="pending")  # pending/running/done/failed
    data_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (Index("ix_run_row_unit", "run_id", "node_id", "row_idx", unique=True),)
