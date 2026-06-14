import re

from app.agent.prompts import (get_coordinator_system_prompt, get_manager_system_prompt,
                               get_worker_system_prompt, load_prompt)
from app.agent.skills import SKILLS_DIR, SkillsManager


def test_render_all_roles():
    sm = SkillsManager(SKILLS_DIR)
    for text in (get_coordinator_system_prompt(sm), get_manager_system_prompt(sm),
                 get_worker_system_prompt(sm), get_worker_system_prompt(sm, parallel=True)):
        assert "gf-cli" in text                      # 技能摘要已注入
        assert not re.search(r"\{[a-z_]+\}", text)   # 无未替换占位符


def test_coordinator_has_goal_and_delete_rules():
    sm = SkillsManager(SKILLS_DIR)
    text = get_coordinator_system_prompt(sm)
    assert "REDLOTUS_GOAL:CONTINUE" in text and "REDLOTUS_GOAL:DONE" in text
    assert "[confirm_delete]" in text


def test_worker_has_report_protocol_and_state_note():
    sm = SkillsManager(SKILLS_DIR)
    text = get_worker_system_prompt(sm, parallel=True)
    assert "SUCCESS:" in text and "FAILED:" in text
    assert "gf use" in text
    assert "report_progress" in text


def test_templates_loadable():
    for name in ("manager_planning_new.md", "manager_planning_continue.md", "manager_summary.md"):
        assert "{user_input}" in load_prompt(name)


def test_new_static_prompts_loadable():
    sysp = load_prompt("codegen_system.md")
    assert "def process(rows: list[dict]) -> list[dict]" in sysp and "output_columns" in sysp
    assert "完整" in sysp or "全部" in sysp                       # 替换语义契约
    assert "pass:false" in load_prompt("qc_empty_anchor.md")      # qc 锚定句
    assert "压缩器" in load_prompt("compactor_system.md")
    for name in ("node_assist_llm_synth.md", "node_assist_qc.md"):
        assert load_prompt(name).strip()


def test_new_templated_prompts_render():
    u = load_prompt("codegen_user.md").format(instruction="去重", columns="q、category")
    assert "去重" in u and "q" in u and "category" in u
    r = load_prompt("goal_round.md").format(
        goal_text="G", run_id=3, metric_str="60.0%", fail_str='[{"q": "a"}]')
    assert "凝练" in r and "60.0%" in r and "G" in r
    f = load_prompt("goal_first_round.md").format(goal_text="G")
    assert "G" in f and "REDLOTUS_GOAL:CONTINUE" in f
