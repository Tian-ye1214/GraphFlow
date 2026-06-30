"""目标循环用的跑数服务：入队一次运行、读首轮指标、抽样失败样本、解析文本阈值。"""
import json
import re
import shutil

from sqlalchemy import delete as sa_delete, func, select

from app.engine.graph import parse_graph, validate_graph
from app.engine.manager import manager
from app.events import publish
from app.models import (Dataset, ModelCallLog, ModelConfig, QcFailure, QcMetric, Run, RunLog,
                        RunNodeState, RunRow, User, Workflow, WorkflowVersion)
from app.services.trace import strip_trace_row

# run 的全部子表（按 run_id 外键）。新增 run 子表只需加到这里，所有删除入口（删单 run / 清空 run /
# 删工作流 / 删用户）自动级联，避免某入口漏删致孤儿数据 + 跨租户泄漏。
RUN_CHILD_MODELS = (RunRow, RunNodeState, RunLog, QcMetric, QcFailure, ModelCallLog)

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_FRAC_RE = re.compile(r"\b(0?\.\d+|1\.0)\b")


async def purge_run_rows(session, run_ids, *, version_ids=None) -> None:
    """级联删除一批 run 的全部子表行 + Run 本身 +（可选）WorkflowVersion 快照。
    只发删除语句、不 commit、不删落盘导出文件（落盘文件由 unlink_run_exports 在 commit 成功后清理，
    以免事务回滚后文件已丢）。空 run_ids 直接返回。"""
    if not run_ids:
        return
    for Model in RUN_CHILD_MODELS:
        await session.execute(sa_delete(Model).where(Model.run_id.in_(run_ids)))
    await session.execute(sa_delete(Run).where(Run.id.in_(run_ids)))
    if version_ids:
        await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.id.in_(version_ids)))


def unlink_run_exports(run_ids, data_dir) -> None:
    """删除一批 run 的落盘文件（commit 成功后调用）：导出文件 + 节点输出 artifact 分片目录 runs/<rid>/。"""
    exports = data_dir / "exports"
    for rid in run_ids:
        for p in exports.glob(f"run{rid}_*"):
            p.unlink(missing_ok=True)
        shutil.rmtree(data_dir / "runs" / str(rid), ignore_errors=True)


def parse_threshold(text: str) -> float | None:
    """从文本目标解析阈值：百分比 90% -> 0.9，小数 0.85 -> 0.85，解析不到 -> None。"""
    m = _PCT_RE.search(text)
    if m:
        return float(m.group(1)) / 100
    m = _FRAC_RE.search(text)
    if m:
        return float(m.group(1))
    return None


def workflow_has_qc(graph) -> bool:
    return any(n.type == "qc" for n in graph.nodes)


async def validate_graph_resource_ownership(session, graph, user_id: int) -> None:
    """逐节点校验图引用的资源(数据集/模型/判定模型)均属 user_id；不符 raise ValueError(点名节点)。
    create_run 起跑前的归属校验单点(防跨租户借草稿盗用他人模型/数据)。"""
    for n in graph.nodes:
        if n.type == "input":
            for ds_id in n.config.get("dataset_ids", []):
                ds = await session.get(Dataset, ds_id)
                if ds is None or ds.user_id != user_id:
                    raise ValueError(f"节点 {n.id}: 数据集不存在")
                if ds.status != "ready":  # 摄入未完成就起跑会静默产 0 行——起跑前拦死，点名报错
                    hint = "仍在导入中，请等待导入完成后再运行" if ds.status == "importing" else "导入失败，请重新上传"
                    raise ValueError(f"节点 {n.id}: 数据集{hint}（status={ds.status}）")
        elif n.type == "llm_synth":
            mc_id = n.config.get("model_config_id")
            mc = await session.get(ModelConfig, mc_id) if mc_id else None
            if mc is None or mc.user_id != user_id:
                raise ValueError(f"节点 {n.id}: 未选择有效的模型配置")
        elif n.type == "qc":
            ids = n.config.get("judge_model_ids") or (
                [n.config["model_config_id"]] if n.config.get("model_config_id") else [])
            if not ids:
                raise ValueError(f"节点 {n.id}: 未选择判定模型")
            for jid in ids:
                mc = await session.get(ModelConfig, jid)
                if mc is None or mc.user_id != user_id:
                    raise ValueError(f"节点 {n.id}: 判定模型无效")


async def enqueue_run(session_factory, user_id: int, workflow_id: int) -> int:
    """快照工作流图为版本并入队一次运行，返回 run_id（不阻塞等待）。"""
    async with session_factory() as s:
        wf = await s.get(Workflow, workflow_id)
        if wf is None or wf.user_id != user_id:
            raise ValueError("工作流不存在")
        graph = parse_graph(wf.graph_json)
        validate_graph(graph)
        # 资源归属/就绪校验：与 create_run 同走单点，挡住目标模式拿 importing/failed 数据集静默空跑（见 fb1a031）
        await validate_graph_resource_ownership(s, graph, user_id)
        max_ver = (await s.execute(select(func.max(WorkflowVersion.version)).where(
            WorkflowVersion.workflow_id == workflow_id))).scalar() or 0
        ver = WorkflowVersion(workflow_id=workflow_id, version=max_ver + 1, graph_json=wf.graph_json)
        s.add(ver)
        await s.flush()
        run = Run(user_id=user_id, workflow_id=workflow_id, workflow_version_id=ver.id)
        s.add(run)
        await s.commit()
        run_id = run.id
        user = await s.get(User, user_id)
        capacity = user.max_llm_concurrency
    manager.submit(run_id, user_id, capacity, session_factory)
    return run_id


async def first_round_rate(session_factory, run_id: int) -> float | None:
    """聚合该运行所有 QC 节点的首轮通过率；无指标返回 None。"""
    async with session_factory() as s:
        rows = (await s.execute(select(QcMetric).where(QcMetric.run_id == run_id))).scalars().all()
    total = sum(m.total for m in rows)
    if not rows or total == 0:
        return None
    return sum(m.first_round_pass for m in rows) / total


async def sample_failures(session_factory, run_id: int, n: int = 20) -> list[dict]:
    """抽样最多 n 条质检失败样本（含各模型理由）。"""
    async with session_factory() as s:
        rows = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id)
                                .order_by(QcFailure.id).limit(n))).scalars().all()
        trace_ids = [f.trace_id for f in rows if f.trace_id]
        logs = []
        if trace_ids:
            logs = (await s.execute(select(ModelCallLog).where(
                ModelCallLog.run_id == run_id,
                ModelCallLog.trace_id.in_(trace_ids),
            ).order_by(ModelCallLog.id))).scalars().all()
    by_trace: dict[str, list[dict]] = {}
    for log in logs:
        by_trace.setdefault(log.trace_id, [])
        if len(by_trace[log.trace_id]) >= 3:
            continue
        response = log.response_json or ""
        by_trace[log.trace_id].append({
            "node_id": log.node_id,
            "source": log.source,
            "model_name": log.model_name,
            "prompt_tokens": log.prompt_tokens,
            "completion_tokens": log.completion_tokens,
            "response": response if len(response) <= 500 else response[:500] + "...<truncated>",
        })
    return [{
        "trace_id": f.trace_id,
        "node_id": f.node_id,
        "sample": strip_trace_row(json.loads(f.sample_json)),
        "reasons": json.loads(f.reasons_json),
        "model_logs": by_trace.get(f.trace_id, []),
    } for f in rows]


async def restore_workflow_from_run(session, run, user_id: int):
    """把 run 快照版本的图覆盖回工作流。他人工作流返回 None；成功 commit + 发 workflow 事件，返回 wf。
    REST 路由与 Agent restore_workflow_from_run 工具共用此单点。"""
    ver = await session.get(WorkflowVersion, run.workflow_version_id)
    wf = await session.get(Workflow, run.workflow_id)
    if wf is None or wf.user_id != user_id:
        return None
    wf.graph_json = ver.graph_json
    await session.commit()
    publish(user_id, "workflow", wf.id)
    return wf
