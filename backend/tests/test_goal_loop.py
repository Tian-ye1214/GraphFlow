import app.agent.goal_loop as gl


def test_should_stop_threshold_hit():
    s = gl.decide(metric=0.92, threshold=0.9, best=0.8, no_improve=0, no_improve_k=2)
    assert s.stop and s.success and "达成" in s.reason


def test_should_stop_no_improve():
    s = gl.decide(metric=0.5, threshold=0.9, best=0.6, no_improve=1, no_improve_k=2)
    assert s.stop and not s.success and "无提升" in s.reason


def test_continue_when_improving():
    s = gl.decide(metric=0.7, threshold=0.9, best=0.6, no_improve=0, no_improve_k=2)
    assert not s.stop and s.new_best == 0.7 and s.new_no_improve == 0


def test_no_threshold_never_threshold_stops():
    s = gl.decide(metric=0.99, threshold=None, best=0.5, no_improve=0, no_improve_k=2)
    assert not s.stop                             # 无阈值不靠指标停（靠 DONE/手动）


def test_round_prompt_includes_metric_and_failures_and_distill():
    p = gl.build_round_prompt("目标X", metric=0.6, failures=[{"sample": {"q": "a"}, "reasons": []}], run_id=5)
    assert "0.6" in p or "60" in p
    assert "凝练" in p and "打补丁" in p           # 凝练经验、非打补丁
    assert "q" in p


def test_goal_prompts_forbid_gaming_data_and_qc():
    """护栏：目标提示词必须禁止 agent 改输入数据/放宽质检判定标准（防 Goodhart 刷分作弊）。"""
    p1 = gl.first_round_prompt("目标X")
    p2 = gl.build_round_prompt("目标X", metric=0.5, failures=[], run_id=1)
    for p in (p1, p2):
        assert "数据集" in p and "判定标准" in p     # 红线点名：考题（数据集）+ 标尺（判定标准）
        assert "作弊" in p
