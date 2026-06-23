import json

from app.agent import goal_loop


def _graph(qc_prompt="strict", dataset_ids=None, gen_prompt="old"):
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": dataset_ids or [1]}},
            {"id": "gen", "type": "llm_synth", "config": {"user_prompt": gen_prompt}},
            {"id": "qc", "type": "qc", "config": {"user_prompt": qc_prompt, "pass_k": 1}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
        ],
    }


def test_goal_guard_allows_non_qc_prompt_changes():
    before = _graph()
    after = _graph(gen_prompt="better")
    assert goal_loop.validate_goal_graph_change(before, after).ok is True


def test_goal_guard_blocks_qc_prompt_changes():
    before = _graph(qc_prompt="strict")
    after = _graph(qc_prompt="loose")
    decision = goal_loop.validate_goal_graph_change(before, after)
    assert decision.ok is False
    assert "QC" in decision.reason


def test_goal_guard_blocks_input_dataset_replacement():
    decision = goal_loop.validate_goal_graph_change(_graph(dataset_ids=[1]),
                                                    _graph(dataset_ids=[2]))
    assert decision.ok is False
    assert "输入数据集" in decision.reason
