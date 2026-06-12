"""AgentSystem：每个回合批次装配一次的三角色系统（coordinator 路由 + Manager 三阶段）。"""
from pathlib import Path

from pydantic_ai.messages import PartDeltaEvent, TextPartDelta

from app.agent.factory import create_agent
from app.agent.orchestrator import TaskManager, WorkerOrchestrator
from app.agent.prompts import (get_coordinator_system_prompt, get_manager_system_prompt,
                               load_prompt)
from app.agent.skills import SKILLS_DIR, SkillsManager, SkillsToolkit
from app.agent.tools import ROLE, AgentToolkit


class AgentSystem:
    """models: {"coordinator"|"manager"|"worker": ModelConfig 或 pydantic-ai Model（测试）}"""

    def __init__(self, *, models: dict, workdir: Path, confirm_delete: bool, emit):
        self.models = models
        self.workdir = Path(workdir)
        self.emit = emit
        self._confirm_delete = confirm_delete
        self.skills_manager = SkillsManager(SKILLS_DIR)
        self.task_manager = TaskManager()
        self._manager_history: list = []
        self._main_state = self.workdir / "cli.json"
        self.orchestrator = WorkerOrchestrator(
            task_manager=self.task_manager, worker_model=models["worker"],
            workdir=self.workdir, make_tools=self._make_tools,
            skills_manager=self.skills_manager)

    def _make_tools(self, state_file: Path) -> list:
        tk = AgentToolkit(self.workdir, state_file, self._confirm_delete)
        sk = SkillsToolkit(self.skills_manager, state_file)
        return tk.tools + sk.tools

    async def run_turn(self, text: str, history: list) -> tuple[list, str]:
        """跑一轮 coordinator，返回 (新的全量历史, 输出文本)。"""
        tools = [self.execute_task_with_manager, self.execute_task_with_worker]
        tools += self._make_tools(self._main_state)
        agent = create_agent(self.models["coordinator"], tools,
                             get_coordinator_system_prompt(self.skills_manager))
        result = await agent.run(text, message_history=history,
                                 event_stream_handler=self._on_stream if self.emit else None)
        return result.all_messages(), str(result.output or "")

    async def _on_stream(self, ctx, events):
        async for ev in events:
            if (isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta)
                    and ev.delta.content_delta):
                await self.emit("delta", ev.delta.content_delta)

    async def execute_task_with_manager(self, user_input: str,
                                        continue_from_previous: bool = False) -> str:
        """把需要规划分解、多子任务并行的复杂请求交给 Manager 执行。
        Manager 会拆解任务清单，系统派多个 Worker 并行执行，最后产出面向用户的最终报告。
        Parameters:
            user_input: 完整的需求描述（新任务）或在上一轮结果上的新要求/反馈（续做）
            continue_from_previous: 是否在上一次 Manager 执行的基础上继续，默认 False
        """
        manager_agent = create_agent(
            self.models["manager"],
            [self.task_manager.create_todo_list, self.task_manager.get_todo_list],
            get_manager_system_prompt(self.skills_manager))

        if continue_from_previous:
            planning = load_prompt("manager_planning_continue.md").format(
                user_input=user_input, current_todo=await self.task_manager.get_todo_list())
        else:
            planning = load_prompt("manager_planning_new.md").format(user_input=user_input)

        token = ROLE.set("manager")
        try:
            result = await manager_agent.run(planning, message_history=self._manager_history)
            self._manager_history = result.all_messages()
        finally:
            ROLE.reset(token)

        final_summary = await self.orchestrator.execute_all_tasks_parallel(user_input)

        summary = load_prompt("manager_summary.md").format(
            user_input=user_input, final_summary=final_summary)
        token = ROLE.set("manager")
        try:
            result = await manager_agent.run(summary, message_history=self._manager_history)
            self._manager_history = result.all_messages()
        finally:
            ROLE.reset(token)
        return str(result.output or "")

    async def execute_task_with_worker(self, task_description: str, user_goal: str = "",
                                       retry_info: str = "") -> str:
        """把单个自包含任务交给一个 Worker 独立执行（带独立 gf 状态与工具沙盒）。
        返回以 SUCCESS:/FAILED: 开头的执行结果。
        Parameters:
            task_description: 清晰具体、自包含的任务描述
            user_goal: 用户的最终目标/大背景，帮 Worker 做取舍
            retry_info: 上次失败的细节（重试时填）
        """
        _success, output = await self.orchestrator.execute_task_with_worker(
            task_description, user_goal, retry_info)
        return output
