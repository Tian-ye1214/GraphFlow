from app.agent.goal import parse_goal


def test_continue():
    sig, text = parse_goal("第1轮推进\n<!-- REDLOTUS_GOAL:CONTINUE -->")
    assert sig == "CONTINUE" and text == "第1轮推进"


def test_done_case_insensitive_with_spaces():
    sig, text = parse_goal("完成 <!--  redlotus_goal : done  -->")
    assert sig == "DONE" and text == "完成"


def test_no_marker():
    sig, text = parse_goal("普通回复")
    assert sig is None and text == "普通回复"


def test_multiple_markers_last_wins_all_stripped():
    sig, text = parse_goal(
        "a <!-- REDLOTUS_GOAL:CONTINUE --> b <!-- REDLOTUS_GOAL:DONE -->")
    assert sig == "DONE"
    assert "REDLOTUS_GOAL" not in text and "a" in text and "b" in text
