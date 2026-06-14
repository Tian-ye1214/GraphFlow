"""节点列血缘：按 topo 顺序静态推算每个节点的输入/输出列集合。"""
import json

from app.engine.graph import Graph, topo_order, upstream_ids
from app.models import Dataset


def _ordered_union(lists: list[list[str]]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for c in lst:
            if c not in out:
                out.append(c)
    return out


def _ordered_intersection(lists: list[list[str]]) -> list[str]:
    """各列表的交集，保第一个列表的顺序。空入参→[]。"""
    if not lists:
        return []
    common = set(lists[0])
    for lst in lists[1:]:
        common &= set(lst)
    return [c for c in lists[0] if c in common]


def _apply_op(cols: list[str], op: dict) -> list[str]:
    kind = op.get("op")
    if kind == "rename":
        mapping = op.get("mapping") or {}
        return [mapping.get(c, c) for c in cols]
    if kind == "drop":
        drop = set(op.get("columns") or [])
        return [c for c in cols if c not in drop]
    if kind == "concat":
        target = op.get("target")
        return cols + [target] if target and target not in cols else cols
    if kind == "agent":
        declared = op.get("output_columns") or []
        return _ordered_union([declared]) if declared else cols
    return cols  # dedup/filter/cast/sample/shuffle 不改列集合


def _node_output(node, input_cols: list[str], dataset_cols: dict[int, list[str]]) -> list[str]:
    t = node.type
    if t == "input":
        # input 节点多数据集=纵向堆叠（行异构）：只保「每行都有」的列=交集，不虚报。
        present = [dataset_cols[d] for d in node.config.get("dataset_ids", []) if d in dataset_cols]
        return _ordered_intersection(present)
    if t == "llm_synth":
        if node.config.get("output_mode") == "json":
            return _ordered_union([input_cols, node.config.get("output_columns") or []])
        return _ordered_union([input_cols, [node.config.get("output_column") or "output"]])
    if t == "auto_process":
        cols = list(input_cols)
        for op in node.config.get("operations") or []:
            cols = _apply_op(cols, op)
        return cols
    return input_cols  # qc / output 透传


def propagate_columns(graph: Graph, dataset_cols: dict[int, list[str]]) -> dict[str, dict]:
    """返回 {node_id: {"input": [...], "output": [...]}}。只沿 normal 边、按 topo 顺序传播。"""
    inputs: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    for node in topo_order(graph):
        in_cols = _ordered_union([outputs.get(uid, []) for uid in upstream_ids(graph, node.id)])
        inputs[node.id] = in_cols
        outputs[node.id] = _node_output(node, in_cols, dataset_cols)
    return {n.id: {"input": inputs[n.id], "output": outputs[n.id]} for n in graph.nodes}


async def resolve_dataset_cols(s, graph: Graph, user_id: int) -> dict[int, list[str]]:
    """取图中所有 input 节点引用、且属于 user_id 的数据集列（租户隔离：非己有跳过）。"""
    ids = {d for n in graph.nodes if n.type == "input" for d in n.config.get("dataset_ids", [])}
    out: dict[int, list[str]] = {}
    for ds_id in ids:
        ds = await s.get(Dataset, ds_id)
        if ds is not None and ds.user_id == user_id:
            out[ds_id] = json.loads(ds.columns_json)
    return out
