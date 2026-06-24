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
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    model_name: Mapped[str] = mapped_column(default="")  # 实际请求用的模型 ID，如 qwen-max
    base_url: Mapped[str]
    provider: Mapped[str] = mapped_column(default="openai")
    azure_api_mode: Mapped[str] = mapped_column(default="legacy")
    api_version: Mapped[str] = mapped_column(default="")
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
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(default="ready")  # importing / ready / failed
    imported_rows: Mapped[int] = mapped_column(default=0)
    import_error: Mapped[str] = mapped_column(Text, default="")  # status=failed 时的错误文案
    original_format: Mapped[str] = mapped_column(default="")
    version: Mapped[int] = mapped_column(default=1)
    version_of_dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), default=None)
    header_row: Mapped[int | None] = mapped_column(default=None)
    data_start_row: Mapped[int] = mapped_column(default=1)
    total_rows_including_header: Mapped[int] = mapped_column(default=0)
    # 来源 run + 节点（save_as_dataset）；上传为 None。按 (run_id,node_id) upsert：同节点 rerun 幂等覆盖，
    # 不同 output 节点即使同名也各自独立（不互相覆盖丢数据）。
    run_id: Mapped[int | None] = mapped_column(default=None)
    node_id: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_datasets_version_parent", "version_of_dataset_id", "version"),)


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
    trace_id: Mapped[str] = mapped_column(default="", index=True)
    file_row: Mapped[int | None] = mapped_column(default=None)
    attempt: Mapped[int] = mapped_column(default=0)
    qc_round: Mapped[int] = mapped_column(default=0)
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="pending")  # pending/running/done/failed
    data_json: Mapped[str] = mapped_column(Text, default="[]")
    output_ref: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        Index("ix_run_row_unit", "run_id", "node_id", "row_idx", unique=True),
        Index("ix_run_row_file_row", "run_id", "node_id", "file_row", unique=True),
        Index("ix_run_row_trace", "run_id", "trace_id"),
    )


class RunLog(Base):
    __tablename__ = "run_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")  # "" 表示运行级事件
    level: Mapped[str] = mapped_column(default="info")  # info / error
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QcMetric(Base):
    __tablename__ = "qc_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    total: Mapped[int] = mapped_column(default=0)
    first_round_pass: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QcFailure(Base):
    __tablename__ = "qc_failures"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(default="")
    trace_id: Mapped[str] = mapped_column(default="", index=True)
    sample_json: Mapped[str] = mapped_column(Text, default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_qc_failure_trace", "run_id", "trace_id"),)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(default="")
    models_json: Mapped[str] = mapped_column(Text)  # {"coordinator": 1, "manager": 1, "worker": 2}
    model_params_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")  # pydantic-ai ModelMessage 全量历史
    status: Mapped[str] = mapped_column(default="idle")  # idle / running
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    role: Mapped[str]  # user / assistant / tool
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelCallLog(Base):
    """模型/Agent 调用日志：网关出口落库。铁律：只记 messages 与响应，绝不记 api_key/Authorization。"""
    __tablename__ = "model_call_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True, default=0)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True, default=None)
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"), index=True, default=None)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("agent_sessions.id"), index=True, default=None)
    node_id: Mapped[str] = mapped_column(default="")
    trace_id: Mapped[str] = mapped_column(default="", index=True)
    source: Mapped[str] = mapped_column(default="")  # synth/qc/redlotus/codegen/assistant/compactor
    model_config_id: Mapped[int | None] = mapped_column(default=None)
    model_name: Mapped[str] = mapped_column(default="")
    provider: Mapped[str] = mapped_column(default="")
    request_json: Mapped[str] = mapped_column(Text, default="[]")
    response_json: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_model_call_log_trace", "run_id", "trace_id"),)


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id"), index=True)
    version: Mapped[int]
    body: Mapped[str] = mapped_column(Text, default="")
    variables_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
