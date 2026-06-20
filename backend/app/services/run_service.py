"""目标循环用的跑数服务：入队一次运行、读首轮指标、抽样失败样本、解析文本阈值。"""
import json
import re

from sqlalchemy import delete as sa_delete, func, select

from app.engine.graph import parse_graph, validate_graph
from app.engine.manager import manager
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow, User,
                        Workflow, WorkflowVersion)

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
    """删除一批 run 的落盘导出文件（commit 成功后调用）。"""
    exports = data_dir / "exports"
    for rid in run_ids:
        for p in exports.glob(f"run{rid}_*"):
            p.unlink(missing_ok=True)


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


async def enqueue_run(session_factory, user_id: int, workflow_id: int) -> int:
    """快照工作流图为版本并入队一次运行，返回 run_id（不阻塞等待）。"""
    async with session_factory() as s:
        wf = await s.get(Workflow, workflow_id)
        if wf is None or wf.user_id != user_id:
            raise ValueError("工作流不存在")
        validate_graph(parse_graph(wf.graph_json))
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
    return [{"sample": json.loads(f.sample_json), "reasons": json.loads(f.reasons_json)}
            for f in rows]
