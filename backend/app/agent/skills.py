"""技能系统：扫描仓库 .claude/skills/<dir>/SKILL.md，供 Agent 现学现用（gf-cli 等）。"""
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.agent.subproc import run_subprocess

SKILLS_DIR = Path(__file__).resolve().parents[3] / ".claude" / "skills"
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SKILL_FILENAME = "SKILL.md"
BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass
class SkillMetadata:
    name: str
    description: str
    path: Path


@dataclass
class Skill:
    metadata: SkillMetadata
    instructions: str = ""

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def path(self) -> Path:
        return self.metadata.path


class SkillsManager:
    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, Skill] = {}
        self.refresh()

    def refresh(self) -> None:
        fresh: dict[str, Skill] = {}
        if self.skills_dir.exists():
            for item in self.skills_dir.iterdir():
                skill_file = item / SKILL_FILENAME
                if not (item.is_dir() and skill_file.is_file()):
                    continue
                parsed = self._parse(skill_file)
                if parsed:
                    fresh[parsed.name] = parsed
        self.skills = fresh

    def _parse(self, skill_file: Path) -> "Skill | None":
        content = skill_file.read_text(encoding="utf-8")
        match = FRONTMATTER_PATTERN.match(content)
        if not match:
            return None
        front = yaml.safe_load(match.group(1))
        if not isinstance(front, dict) or not front.get("name"):
            return None
        meta = SkillMetadata(name=str(front["name"]), description=str(front.get("description", "")),
                             path=skill_file.parent)
        return Skill(metadata=meta, instructions=content[match.end():].strip())

    def get_all_metadata(self) -> list[SkillMetadata]:
        return [self.skills[n].metadata for n in sorted(self.skills)]

    def get_skills_summary(self) -> str:
        if not self.skills:
            return "当前没有可用的 Skills。"
        lines = ["## 可用的 Agent Skills", ""]
        lines += [f"- **{m.name}**: {m.description}" for m in self.get_all_metadata()]
        lines.append("")
        lines.append("用 `get_skill_instructions(skill_name)` 获取具体 Skill 的详细指令。")
        return "\n".join(lines)

    def load_skill_instructions(self, name: str) -> "str | None":
        skill = self.skills.get(name)
        return skill.instructions if skill else None

    def list_skill_resources(self, skill_name: str) -> list[str]:
        skill = self.skills.get(skill_name)
        if not skill:
            return []
        return [str(p.relative_to(skill.path)) for p in skill.path.rglob("*")
                if p.is_file() and p.name != SKILL_FILENAME]

    def load_skill_resource(self, skill_name: str, resource_name: str) -> "str | None":
        skill = self.skills.get(skill_name)
        if not skill:
            return None
        target = self._resolve_within(skill.path, resource_name)
        if target is None or not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _resolve_within(skill_dir: Path, relative: str) -> "Path | None":
        base = skill_dir.resolve()
        try:
            target = (base / relative).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(base) else None


class SkillsToolkit:
    """技能工具集；execute_skill_script 注入会话 GF_STATE_FILE，脚本里可直接用 gf。"""

    def __init__(self, manager: SkillsManager, state_file: Path):
        self._manager = manager
        self._state_file = state_file

    async def list_available_skills(self) -> str:
        """列出所有可用的 Agent Skills（名称与描述）。执行复杂任务前先看有哪些技能可用。"""
        metas = self._manager.get_all_metadata()
        if not metas:
            return "当前没有可用的 Skills。"
        lines = [f"{i}. {m.name} —— {m.description}" for i, m in enumerate(metas, 1)]
        lines.append("用 get_skill_instructions(skill_name) 获取详细指令。")
        return "\n".join(lines)

    async def get_skill_instructions(self, skill_name: str) -> str:
        """获取指定 Skill 的完整指令（工作流程、键名表、示例）。使用技能前的必要步骤。
        Parameters:
            skill_name: Skill 名称（如 "gf-cli"）
        """
        instructions = self._manager.load_skill_instructions(skill_name)
        if instructions is None:
            names = ", ".join(m.name for m in self._manager.get_all_metadata()) or "无"
            return f"错误: Skill '{skill_name}' 不存在。可用: {names}"
        resources = self._manager.list_skill_resources(skill_name)
        out = [f"# Skill: {skill_name}", instructions]
        if resources:
            out.append("可用的额外资源: " + ", ".join(resources)
                       + "\n用 load_skill_resource(skill_name, resource_name) 加载。")
        return "\n\n".join(out)

    async def load_skill_resource(self, skill_name: str, resource_name: str) -> str:
        """加载 Skill 的额外资源文件（参考文档、脚本等），按需加载避免一次塞满上下文。
        Parameters:
            skill_name: Skill 名称
            resource_name: 资源文件名（如 "reference.md", "scripts/x.ps1"）
        """
        content = self._manager.load_skill_resource(skill_name, resource_name)
        if content is None:
            return f"错误: 资源 '{resource_name}' 不存在或越界"
        return content

    async def refresh_skills(self) -> str:
        """重新扫描技能目录，发现新增或更新的 Skills。"""
        self._manager.refresh()
        return f"Skills 已刷新，当前 {len(self._manager.skills)} 个可用。"

    async def execute_skill_script(self, skill_name: str, script_name: str,
                                   args: str = "", timeout: float = 300) -> str:
        """执行 Skill 内的脚本（.py/.ps1/.bat/.sh），返回输出；脚本代码本身不进上下文。
        Parameters:
            skill_name: Skill 名称
            script_name: 脚本相对路径（仅限技能目录内）
            args: 传给脚本的参数（按空格切分，不支持引号包含空格）
            timeout: 最长执行秒数，默认 300
        """
        skill = self._manager.skills.get(skill_name)
        if not skill:
            return f"错误: Skill '{skill_name}' 不存在"
        script = SkillsManager._resolve_within(skill.path, script_name)
        if script is None or not script.exists():
            return f"错误: 脚本 '{script_name}' 不存在或越界"
        executors = {".py": [sys.executable], ".sh": ["bash"],
                     ".bat": ["cmd", "/c"], ".ps1": ["powershell", "-File"]}
        ext = script.suffix.lower()
        if ext not in executors:
            return f"错误: 不支持的脚本类型 '{ext}'"
        cmd = executors[ext] + [str(script)] + (args.split() if args else [])
        # PYTHONIOENCODING：Windows 管道默认 cp936，子进程打印中文会乱码
        env = {**os.environ, "GF_STATE_FILE": str(self._state_file),
               "PYTHONPATH": str(BACKEND_DIR), "PYTHONIOENCODING": "utf-8"}
        import subprocess
        try:
            stdout, stderr, code = await run_subprocess(
                cmd, shell=False, cwd=str(skill.path), env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"错误: 脚本执行超时（{timeout} 秒）"
        output = stdout + stderr
        return f"返回码: {code}\n输出:\n{output}" if output else f"执行完成，返回码: {code}"

    @property
    def tools(self) -> list:
        return [self.list_available_skills, self.get_skill_instructions,
                self.load_skill_resource, self.refresh_skills, self.execute_skill_script]
