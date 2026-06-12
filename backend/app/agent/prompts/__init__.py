"""提示词加载与渲染。模板占位符：current_time / skills_summary / common_conduct。"""
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _render(name: str, skills_manager) -> str:
    return load_prompt(name).format(
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        skills_summary=skills_manager.get_skills_summary(),
        common_conduct=load_prompt("common_conduct.md"),
    )


def get_coordinator_system_prompt(skills_manager) -> str:
    return _render("coordinator_system.md", skills_manager)


def get_manager_system_prompt(skills_manager) -> str:
    return _render("manager_system.md", skills_manager)


def get_worker_system_prompt(skills_manager, parallel: bool = False) -> str:
    text = _render("worker_system.md", skills_manager)
    return text + load_prompt("worker_parallel_addon.md") if parallel else text
