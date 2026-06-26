"""工作流落库/删除 + 名/ID 解析的服务单点。workflows 路由与 Agent GraphToolkit 共用，
保证「落库 + 发 workflow SSE 事件」「级联删除」各走一条路径（画布据事件调和、子表零孤儿）。"""
import json

from sqlalchemy import delete as sa_delete, select

from app.events import publish
from app.models import Dataset, ModelCallLog, ModelConfig, Prompt, Run, Workflow, WorkflowVersion
from app.services.graph_ops import GraphOpError
from app.services.run_service import purge_run_rows, unlink_run_exports

_KIND_MODEL = {"workflows": Workflow, "datasets": Dataset, "models": ModelConfig, "prompts": Prompt}
_KIND_LABEL = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置", "prompts": "提示词"}


async def update_workflow_graph(session, wf: Workflow, graph: dict) -> None:
    wf.graph_json = json.dumps(graph, ensure_ascii=False)
    await session.commit()
    publish(wf.user_id, "workflow", wf.id)


async def delete_workflow_full(session, wf: Workflow, data_dir) -> None:
    """删工作流 + 级联清全部子表(run 子表/run/节点助手日志/版本快照) + 回收磁盘导出 + 发 workflow 事件。
    调用方须先确保无运行中的 run（运行中守卫各入口自行映射错误：REST→409，Agent→错误串）。
    REST 删除路由与 Agent GraphToolkit.delete_workflow 共用此删除单点，避免某入口漏删致孤儿/泄漏。"""
    uid, wid = wf.user_id, wf.id
    run_ids = list((await session.execute(
        select(Run.id).where(Run.workflow_id == wid))).scalars().all())
    await purge_run_rows(session, run_ids)
    # 节点助手类日志（无 run、按 workflow 归属）一并清
    await session.execute(sa_delete(ModelCallLog).where(ModelCallLog.workflow_id == wid))
    await session.execute(sa_delete(WorkflowVersion).where(WorkflowVersion.workflow_id == wid))
    await session.delete(wf)
    await session.commit()
    unlink_run_exports(run_ids, data_dir)
    publish(uid, "workflow", wid)


async def resolve_ref(session, user_id: int, kind: str, ref) -> int:
    """纯数字按 id（仍校验归属），否则按 name 精确匹配本租户资源。0/多个匹配抛 GraphOpError。"""
    model = _KIND_MODEL[kind]
    s = str(ref)
    if s.isdigit():
        obj = await session.get(model, int(s))
        if obj is None or obj.user_id != user_id:
            raise GraphOpError(f"找不到 id={s} 的{_KIND_LABEL[kind]}")
        return int(s)
    hits = (await session.execute(
        select(model).where(model.user_id == user_id, model.name == s))).scalars().all()
    if len(hits) == 1:
        return hits[0].id
    if not hits:
        raise GraphOpError(f"找不到名为「{s}」的{_KIND_LABEL[kind]}")
    raise GraphOpError(f"「{s}」有 {len(hits)} 个同名{_KIND_LABEL[kind]}，请改用 id: {[h.id for h in hits]}")
