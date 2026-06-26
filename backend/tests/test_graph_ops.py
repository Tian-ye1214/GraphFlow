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


def _node(t="llm_synth"):
    return {"id": "n", "type": t, "config": {}}


def test_apply_llm_config_and_params():
    n = _node()
    go.apply_node_config(n, "prompt", "你好 {{q}}")
    go.apply_node_config(n, "out", "ans")
    go.apply_node_config(n, "fanout", "2")
    go.apply_node_config(n, "temp", "0.7")
    c = n["config"]
    assert c["user_prompt"] == "你好 {{q}}" and c["output_column"] == "ans"
    assert c["fanout_n"] == 2 and c["params"]["temperature"] == 0.7


def test_apply_resolve_keys_expect_ids():
    n = _node()
    go.apply_node_config(n, "model", 7)            # 已解析 id
    go.apply_node_config(n, "dataset", [3, 4])     # 已解析 id 列表
    assert n["config"]["model_config_id"] == 7 and n["config"]["dataset_ids"] == [3, 4]


def test_apply_extract_dict_or_string():
    n = _node("http_fetch")
    go.apply_node_config(n, "extract", "who:name,yr:age")
    assert n["config"]["extract"] == {"who": "name", "yr": "age"}
    go.apply_node_config(n, "extract", {"x": "y"})  # dict 直接用
    assert n["config"]["extract"] == {"x": "y"}


def test_apply_count_empty_means_none():
    n = _node("output")
    go.apply_node_config(n, "count", "5")
    assert n["config"]["count"] == 5
    go.apply_node_config(n, "count", "")
    assert n["config"]["count"] is None


def test_apply_think_and_unknown_key():
    n = _node()
    go.apply_node_config(n, "think", "on")
    assert n["config"]["params"]["thinking_enabled"] is True
    with pytest.raises(go.GraphOpError, match="未知配置键"):
        go.apply_node_config(n, "nope", "x")


def test_add_and_remove_op():
    n = _node("auto_process")
    op = go.add_op(n, "dedup", ["q,a"])
    assert op == {"op": "dedup", "columns": ["q", "a"]}
    assert n["config"]["operations"] == [op]
    removed = go.remove_op(n, 1)
    assert removed["op"] == "dedup" and n["config"]["operations"] == []


def test_add_op_non_auto_raises():
    with pytest.raises(go.GraphOpError, match="auto"):
        go.add_op(_node("llm_synth"), "shuffle", [])


def test_remove_op_index_out_of_range():
    n = _node("auto_process")
    go.add_op(n, "shuffle", [])
    with pytest.raises(go.GraphOpError, match="序号"):
        go.remove_op(n, 5)
