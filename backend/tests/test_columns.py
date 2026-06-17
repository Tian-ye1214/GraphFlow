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


def test_auto_process_agent_op_replaces_with_declared_columns():
    """agent 操作声明=运行后的完整列集合（替换，非并入）：声明 q_english 即只剩 q_english。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ap"]["output"] == ["q_english"]


def test_auto_process_agent_op_empty_declaration_passthrough():
    """未声明产出列（[]）→ 透传输入，不静默造列。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": []}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ap"]["output"] == ["id", "q", "category"]


def test_workflow2_delete_all_keep_one():
    """复刻 workflow 2：llm column→q_english 后接 agent 替换为 [q_english]，下游 output 只见 q_english。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_mode": "column", "output_column": "q_english"}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "ap", "kind": "normal"},
         {"source": "ap", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ap"]["output"] == ["q_english"]
    assert cols["out"]["input"] == ["q_english"]


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
    # 多父节点=并行分支汇合(merge)：每行并入各支列 → 血缘取并集（执行端按行合并，union 如实）
    g = _g(
        [{"id": "a", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "b", "type": "input", "config": {"dataset_ids": [2]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "a", "target": "out", "kind": "normal"},
         {"source": "b", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"], 2: ["id", "x"]})
    assert cols["out"]["input"] == ["id", "q", "x"]


def test_input_multi_dataset_uses_intersection():
    """单 input 节点选多数据集=纵向堆叠(行异构)：血缘取交集（每行都有的列），不虚报并集。"""
    g = _g([{"id": "in", "type": "input", "config": {"dataset_ids": [1, 2]}},
            {"id": "out", "type": "output", "config": {}}],
           [{"source": "in", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"], 2: ["id", "text"]})
    assert cols["in"]["output"] == ["id"]
    assert cols["out"]["input"] == ["id"]


def test_http_fetch_adds_extract_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "h", "type": "http_fetch",
          "config": {"extract": {"temp": "data.temp", "desc": "data.weather.0.desc"}}}],
        [{"source": "in", "target": "h", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"]})
    assert cols["h"]["output"] == ["id", "q", "temp", "desc"]


def test_http_fetch_no_extract_passthrough():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "h", "type": "http_fetch", "config": {"url": "http://x"}}],
        [{"source": "in", "target": "h", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q"]})
    assert cols["h"]["output"] == ["id", "q"]


def test_drop_columns_removed_from_output_and_downstream():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth",
          "config": {"output_column": "a", "drop_columns": ["secret"]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "secret"]})
    assert cols["ls"]["output"] == ["id", "q", "a"]   # secret 被本节点删除
    assert cols["out"]["input"] == ["id", "q", "a"]   # 下游看不到 secret


def test_drop_columns_empty_is_noop():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_column": "a"}}],
        [{"source": "in", "target": "ls", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ls"]["output"] == ["q", "a"]          # 无 drop_columns 时行为不变


def test_empty_graph():
    assert propagate_columns(parse_graph({"nodes": [], "edges": []}), {}) == {}
