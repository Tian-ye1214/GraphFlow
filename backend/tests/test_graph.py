import pytest

from app.engine.graph import GraphError, parse_graph, topo_order, upstream_ids, validate_graph


def g(nodes, edges):
    return parse_graph({
        "nodes": [{"id": i, "type": t, "config": {}} for i, t in nodes],
        "edges": [{"source": s, "target": t, "kind": k} for s, t, k in edges],
    })


LINEAR = [("a", "input"), ("b", "llm_synth"), ("c", "output")]


def test_topo_linear():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal")])
    assert [n.id for n in topo_order(graph)] == ["a", "b", "c"]


def test_topo_dag_branch_merge():
    nodes = LINEAR + [("d", "auto_process")]
    edges = [("a", "b", "normal"), ("a", "d", "normal"), ("b", "c", "normal"), ("d", "c", "normal")]
    order = [n.id for n in topo_order(g(nodes, edges))]
    assert order.index("a") < order.index("b") < order.index("c")
    assert order.index("a") < order.index("d") < order.index("c")


def test_cycle_rejected():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal"), ("c", "a", "normal")])
    with pytest.raises(GraphError, match="环"):
        topo_order(graph)


def test_validate_unknown_type():
    with pytest.raises(GraphError, match="未知节点类型"):
        validate_graph(g([("a", "magic")], []))


def test_validate_dangling_edge():
    with pytest.raises(GraphError, match="不存在的节点"):
        validate_graph(g(LINEAR, [("a", "nope", "normal")]))


def test_validate_duplicate_id():
    with pytest.raises(GraphError, match="重复"):
        validate_graph(g([("a", "input"), ("a", "output")], []))


def test_upstream_ids():
    graph = g(LINEAR, [("a", "b", "normal"), ("b", "c", "normal")])
    assert upstream_ids(graph, "c") == ["b"]
    assert upstream_ids(graph, "a") == []


def test_parse_malformed_raises_graph_error():
    with pytest.raises(GraphError, match="缺少字段"):
        parse_graph({"nodes": [{"type": "input"}], "edges": []})
