from app.agent.skills import SKILLS_DIR, SkillsManager, SkillsToolkit


def test_discovers_gf_cli_skill():
    sm = SkillsManager(SKILLS_DIR)
    assert "gf-cli" in [m.name for m in sm.get_all_metadata()]
    assert "gf-cli" in sm.get_skills_summary()


def test_load_instructions_and_resources():
    sm = SkillsManager(SKILLS_DIR)
    assert "gf" in sm.load_skill_instructions("gf-cli")
    resources = sm.list_skill_resources("gf-cli")
    res = next((r for r in resources if "build-pipeline.ps1" in r), None)
    assert res is not None
    assert sm.load_skill_resource("gf-cli", res)


def test_resource_escape_blocked():
    sm = SkillsManager(SKILLS_DIR)
    assert sm.load_skill_resource("gf-cli", "../../backend/app/config.py") is None


async def test_toolkit_unknown_skill(tmp_path):
    tk = SkillsToolkit(SkillsManager(SKILLS_DIR), tmp_path / "cli.json")
    assert "不存在" in await tk.get_skill_instructions("no-such-skill")


async def test_execute_script_unsupported_ext(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody", encoding="utf-8")
    (skill_dir / "x.exe").write_bytes(b"")
    tk = SkillsToolkit(SkillsManager(tmp_path / "skills"), tmp_path / "cli.json")
    assert "不支持" in await tk.execute_skill_script("demo", "x.exe")


async def test_execute_script_injects_gf_state(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody", encoding="utf-8")
    (skill_dir / "show_env.py").write_text(
        "import os; print(os.environ.get('GF_STATE_FILE', 'MISSING'))", encoding="utf-8")
    state = tmp_path / "cli.json"
    tk = SkillsToolkit(SkillsManager(tmp_path / "skills"), state)
    out = await tk.execute_skill_script("demo", "show_env.py")
    assert str(state) in out
