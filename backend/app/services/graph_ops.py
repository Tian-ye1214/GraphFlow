"""GraphFlow 图变更纯函数：对 graph dict（{"nodes":[...], "edges":[...]}）做结构/配置变更，
不碰 DB。失败抛 GraphOpError。gf CLI 与 Agent GraphToolkit 共用此单点（去重）。"""


class GraphOpError(ValueError):
    """图变更非法（节点不存在、类型未知、边重复、未知配置键等）。调用方 catch 后 die/返回错误串。
    是 ValueError 子类，故 gf CLI 顶层 except ValueError→die 会自动把它转成命令行错误。"""


NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "qc": "qc", "llm_synth": "llm_synth", "auto_process": "auto_process",
              "http": "http_fetch", "http_fetch": "http_fetch"}


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    raise GraphOpError(f"节点 {node_id} 不存在")


def add_node(graph: dict, node_type: str, node_id: str | None = None) -> str:
    ntype = NODE_TYPES.get(node_type)
    if ntype is None:
        raise GraphOpError(f"未知节点类型 {node_type}（可选: input/llm/auto/output/qc/http）")
    nodes = graph["nodes"]
    if node_id:
        if any(n["id"] == node_id for n in nodes):
            raise GraphOpError(f"节点 {node_id} 已存在")
    else:
        i = 1
        while any(n["id"] == f"{ntype}_{i}" for n in nodes):
            i += 1
        node_id = f"{ntype}_{i}"
    nodes.append({"id": node_id, "type": ntype,
                  "position": {"x": 80 + len(nodes) * 50, "y": 80 + len(nodes) * 40},
                  "config": {}})
    return node_id


def remove_node(graph: dict, node_id: str) -> None:
    find_node(graph, node_id)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]
    graph["edges"] = [e for e in graph["edges"] if node_id not in (e["source"], e["target"])]


def connect(graph: dict, source: str, target: str, kind: str) -> None:
    src = find_node(graph, source)
    find_node(graph, target)
    if kind == "rescan" and src["type"] != "qc":
        raise GraphOpError("rescan 回扫边必须从 qc 节点出发")
    if any(e["source"] == source and e["target"] == target for e in graph["edges"]):
        raise GraphOpError("连线已存在")
    graph["edges"].append({"source": source, "target": target, "kind": kind})


def disconnect(graph: dict, source: str, target: str) -> None:
    before = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"]
                      if not (e["source"] == source and e["target"] == target)]
    if len(graph["edges"]) == before:
        raise GraphOpError(f"不存在连线 {source} -> {target}")
