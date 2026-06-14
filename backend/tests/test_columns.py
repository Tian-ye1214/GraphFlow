from app.engine.columns import propagate_columns
from app.engine.graph import parse_graph


def _g(nodes, edges):
    return parse_graph({"nodes": nodes, "edges": edges})


def test_input_node_outputs_dataset_columns():
    g = _g([{"id": "in", "type": "input", "config": {"dataset_ids": [1]}}], [])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["in"]["output"] == ["id", "q", "category"]
    assert cols["in"]["input"] == []


def test_llm_synth_column_mode_adds_output_column():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_mode": "column", "output_column": "a"}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ls"]["output"] == ["id", "q", "category", "a"]


def test_llm_synth_column_mode_defaults_to_output():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ls"]["output"] == ["q", "output"]


def test_llm_synth_json_mode_uses_declared_output_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth",
          "config": {"output_mode": "json", "output_columns": ["q_en", "category_en"]}},
         {"id": "qc", "type": "qc", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "qc", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ls"]["output"] == ["id", "q", "category", "q_en", "category_en"]
    assert cols["qc"]["input"] == ["id", "q", "category", "q_en", "category_en"]


def test_auto_process_agent_op_adds_declared_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ap"]["output"] == ["q", "q_english"]


def test_auto_process_rename_drop_concat():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process", "config": {"operations": [
             {"op": "rename", "mapping": {"q": "question"}},
             {"op": "drop", "columns": ["category"]},
             {"op": "concat", "target": "merged", "columns": ["question"], "sep": "-"}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q", "category"]})
    assert cols["ap"]["output"] == ["question", "merged"]


def test_qc_passthrough_and_rescan_ignored():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_column": "a"}},
         {"id": "qc", "type": "qc", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "qc", "kind": "normal"},
         {"source": "qc", "target": "ls", "kind": "rescan"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["qc"]["output"] == ["q", "a"]


def test_ordered_union_dedupes_across_upstreams():
    g = _g(
        [{"id": "a", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "b", "type": "input", "config": {"dataset_ids": [2]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "a", "target": "out", "kind": "normal"},
         {"source": "b", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"], 2: ["id", "x"]})
    assert cols["out"]["input"] == ["id", "q", "x"]


def test_empty_graph():
    assert propagate_columns(parse_graph({"nodes": [], "edges": []}), {}) == {}
