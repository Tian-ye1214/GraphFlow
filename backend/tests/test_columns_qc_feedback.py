from app.engine.columns import propagate_columns
from app.engine.graph import parse_graph


def test_qc_adds_default_feedback_column():
    graph = parse_graph({
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
            {"id": "qc", "type": "qc", "config": {}},
            {"id": "llm", "type": "llm_synth", "config": {"output_column": "answer"}},
        ],
        "edges": [
            {"source": "in", "target": "qc"},
            {"source": "qc", "target": "llm"},
        ],
    })

    cols = propagate_columns(graph, {1: ["q", "answer"]})

    assert cols["qc"]["output"] == ["q", "answer", "qc_feedback"]
    assert cols["llm"]["input"] == ["q", "answer", "qc_feedback"]


def test_qc_uses_custom_feedback_column():
    graph = parse_graph({
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
            {"id": "qc", "type": "qc", "config": {"feedback_column": "review_note"}},
        ],
        "edges": [{"source": "in", "target": "qc"}],
    })

    cols = propagate_columns(graph, {1: ["q", "answer"]})

    assert cols["qc"]["output"] == ["q", "answer", "review_note"]


def test_qc_drop_columns_can_hide_feedback_column():
    graph = parse_graph({
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
            {
                "id": "qc",
                "type": "qc",
                "config": {"feedback_column": "review_note", "drop_columns": ["review_note"]},
            },
        ],
        "edges": [{"source": "in", "target": "qc"}],
    })

    cols = propagate_columns(graph, {1: ["q", "answer"]})

    assert cols["qc"]["output"] == ["q", "answer"]
