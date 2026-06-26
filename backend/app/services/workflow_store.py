"""工作流落库 + 名/ID 解析的服务单点。workflows 路由 PUT 与 Agent GraphToolkit 共用，
保证「落库 + 发 workflow SSE 事件」一条路径（画布据事件调和）。"""
import json

from sqlalchemy import select

from app.events import publish
from app.models import Dataset, ModelConfig, Prompt, Workflow
from app.services.graph_ops import GraphOpError

_KIND_MODEL = {"workflows": Workflow, "datasets": Dataset, "models": ModelConfig, "prompts": Prompt}
_KIND_LABEL = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置", "prompts": "提示词"}


async def update_workflow_graph(session, wf: Workflow, graph: dict) -> None:
    wf.graph_json = json.dumps(graph, ensure_ascii=False)
    await session.commit()
    publish(wf.user_id, "workflow", wf.id)


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
