import pytest
from app.services import graph_ops as go


def _g():
    return {"nodes": [], "edges": []}


def test_add_node_autoid_and_default_shape():
    g = _g()
    nid = go.add_node(g, "llm")
    assert nid == "llm_synth_1"
    n = g["nodes"][0]
    assert n["id"] == "llm_synth_1" and n["type"] == "llm_synth" and n["config"] == {}
    assert set(n["position"]) == {"x", "y"}


def test_add_node_explicit_id_dup_raises():
    g = _g()
    go.add_node(g, "input", "in")
    with pytest.raises(go.GraphOpError, match="已存在"):
        go.add_node(g, "input", "in")


def test_add_node_unknown_type_raises():
    with pytest.raises(go.GraphOpError, match="未知节点类型"):
        go.add_node(_g(), "banana")


def test_remove_node_drops_incident_edges():
    g = {"nodes": [{"id": "a", "type": "input", "config": {}},
                   {"id": "b", "type": "output", "config": {}}],
         "edges": [{"source": "a", "target": "b", "kind": "normal"}]}
    go.remove_node(g, "a")
    assert [n["id"] for n in g["nodes"]] == ["b"]
    assert g["edges"] == []


def test_remove_node_missing_raises():
    with pytest.raises(go.GraphOpError):
        go.remove_node(_g(), "x")


def test_connect_normal_and_dup_raises():
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "output", "config": {}}], "edges": []}
    go.connect(g, "a", "b", "normal")
    assert g["edges"] == [{"source": "a", "target": "b", "kind": "normal"}]
    with pytest.raises(go.GraphOpError, match="已存在"):
        go.connect(g, "a", "b", "normal")


def test_connect_rescan_must_start_from_qc():
    g = {"nodes": [{"id": "a", "type": "llm_synth", "config": {}},
                   {"id": "b", "type": "llm_synth", "config": {}}], "edges": []}
    with pytest.raises(go.GraphOpError, match="qc"):
        go.connect(g, "a", "b", "rescan")


def test_disconnect_removes_and_missing_raises():
    g = {"nodes": [], "edges": [{"source": "a", "target": "b", "kind": "normal"}]}
    go.disconnect(g, "a", "b")
    assert g["edges"] == []
    with pytest.raises(go.GraphOpError):
        go.disconnect(g, "a", "b")
