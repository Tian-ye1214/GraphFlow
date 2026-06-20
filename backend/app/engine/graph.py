import json
from dataclasses import dataclass

NODE_TYPES = {"input", "llm_synth", "auto_process", "output", "qc", "http_fetch"}


class GraphError(ValueError):
    pass


@dataclass
class Node:
    id: str
    type: str
    config: dict


@dataclass
class Graph:
    nodes: list[Node]
    edges: list[dict]  # {"source", "target", "kind": "normal"|"rescan"}


def parse_graph(graph_json: str | dict) -> Graph:
    data = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
    try:
        nodes = [Node(id=n["id"], type=n["type"], config=n.get("config", {})) for n in data.get("nodes", [])]
        edges = [{"source": e["source"], "target": e["target"], "kind": e.get("kind", "normal")}
                 for e in data.get("edges", [])]
    except KeyError as exc:
        raise GraphError(f"图数据结构损坏: 缺少字段 {exc}") from exc
    return Graph(nodes=nodes, edges=edges)


def validate_graph(g: Graph) -> None:
    ids = [n.id for n in g.nodes]
    if len(ids) != len(set(ids)):
        raise GraphError("节点 id 重复")
    id_set = set(ids)
    for n in g.nodes:
        if n.type not in NODE_TYPES:
            raise GraphError(f"未知节点类型: {n.type}")
    qc_ids = {n.id for n in g.nodes if n.type == "qc"}
    for e in g.edges:
        if e["source"] not in id_set or e["target"] not in id_set:
            raise GraphError("边指向不存在的节点")
        if e["kind"] == "rescan" and e["source"] not in qc_ids:
            raise GraphError("rescan 回扫边必须从 qc 节点出发")
    topo_order(g)


def topo_order(g: Graph) -> list[Node]:
    """Kahn 算法，仅按 normal 边；有环抛 GraphError。"""
    normal = [e for e in g.edges if e["kind"] == "normal"]
    by_id = {n.id: n for n in g.nodes}
    indeg = {n.id: 0 for n in g.nodes}
    for e in normal:
        indeg[e["target"]] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(by_id[nid])
        for e in normal:
            if e["source"] == nid:
                indeg[e["target"]] -= 1
                if indeg[e["target"]] == 0:
                    queue.append(e["target"])
    if len(order) != len(g.nodes):
        raise GraphError("工作流包含环（普通边必须无环）")
    return order


def upstream_ids(g: Graph, node_id: str) -> list[str]:
    return [e["source"] for e in g.edges if e["target"] == node_id and e["kind"] == "normal"]


def descendants(g: Graph, node_id: str) -> set[str]:
    """沿 normal 边可达的所有下游节点 id（不含自身）。"""
    out: set[str] = set()
    frontier = [node_id]
    while frontier:
        nid = frontier.pop()
        for e in g.edges:
            if e["kind"] == "normal" and e["source"] == nid and e["target"] not in out:
                out.add(e["target"])
                frontier.append(e["target"])
    return out


def ancestors(g: Graph, node_id: str) -> set[str]:
    """沿 normal 边可达的所有上游节点 id（不含自身）。"""
    out: set[str] = set()
    frontier = [node_id]
    while frontier:
        nid = frontier.pop()
        for e in g.edges:
            if e["kind"] == "normal" and e["target"] == nid and e["source"] not in out:
                out.add(e["source"])
                frontier.append(e["source"])
    return out
