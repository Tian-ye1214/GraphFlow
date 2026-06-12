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
