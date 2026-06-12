"""任务清单 + Worker 并行波次编排（RedLotus WorkerOrchestrator 精简移植）。"""
import asyncio
import json
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.agent.factory import create_agent
from app.agent.prompts import get_worker_system_prompt
from app.agent.tools import ROLE

MAX_WORKER_CONCURRENT = 3
MAX_WAVES = 15


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    retry_count: int = 0
    max_retries: int = 3
    dependencies: list[str] = field(default_factory=list)
    failure_history: list[str] = field(default_factory=list)
    history: list = field(default_factory=list)  # Worker 的 ModelMessage 历史，跨重试沿用


class TaskManager:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.task_order: list[str] = []

    async def create_todo_list(self, tasks_json: str) -> str:
        """从 JSON 创建任务清单（覆盖旧清单）。
        Parameters:
            tasks_json: JSON 数组，格式 [{"id": "1", "description": "任务描述", "dependencies": ["依赖的任务 id"]}]
        """
        try:
            data = json.loads(tasks_json)
        except json.JSONDecodeError as e:
            return f"错误: JSON 解析失败 - {e}"
        if not isinstance(data, list) or not data:
            return "错误: 应为非空 JSON 数组"
        err = self._validate(data)
        if err:
            return f"错误: {err}"
        self.tasks.clear()
        self.task_order.clear()
        for i, td in enumerate(data):
            tid = str(td.get("id", i + 1)).strip()
            self.tasks[tid] = Task(id=tid, description=str(td["description"]).strip(),
                                   dependencies=[str(d).strip() for d in td.get("dependencies") or []])
            self.task_order.append(tid)
        return self._format()

    async def get_todo_list(self) -> str:
        """查看当前任务清单状态。"""
        return self._format()

    @staticmethod
    def _validate(data: list) -> str:
        seen: set[str] = set()
        deps_by_id: dict[str, list[str]] = {}
        for i, td in enumerate(data):
            if not isinstance(td, dict) or not str(td.get("description", "")).strip():
                return f"第 {i + 1} 项缺少 description"
            tid = str(td.get("id", i + 1)).strip()
            if tid in seen:
                return f"重复的任务 id: {tid}"
            seen.add(tid)
            deps_by_id[tid] = [str(d).strip() for d in td.get("dependencies") or []]
        for tid, deps in deps_by_id.items():
            for d in deps:
                if d not in seen:
                    return f"任务 {tid} 有未知依赖: {d}"
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(tid: str) -> str:
            if tid in done:
                return ""
            if tid in visiting:
                return f"检测到循环依赖（涉及任务 {tid}）"
            visiting.add(tid)
            for d in deps_by_id[tid]:
                if err := visit(d):
                    return err
            visiting.discard(tid)
            done.add(tid)
            return ""

        for tid in deps_by_id:
            if err := visit(tid):
                return err
        return ""

    def _format(self) -> str:
        if not self.tasks:
            return "任务清单为空"
        icons = {TaskStatus.PENDING: "⬜", TaskStatus.IN_PROGRESS: "🔄",
                 TaskStatus.COMPLETED: "✅", TaskStatus.FAILED: "❌"}
        lines = []
        for tid in self.task_order:
            t = self.tasks[tid]
            line = f"{icons[t.status]} [{t.id}] {t.description}"
            if t.dependencies:
                line += f"（依赖: {', '.join(t.dependencies)}）"
            if t.retry_count:
                line += f"[重试 {t.retry_count}/{t.max_retries}]"
            lines.append(line)
        completed = sum(1 for t in self.tasks.values() if t.status is TaskStatus.COMPLETED)
        lines.append(f"进度: {completed}/{len(self.tasks)}")
        return "\n".join(lines)

    def get_all_ready_tasks(self) -> list[Task]:
        ready = []
        for tid in self.task_order:
            t = self.tasks[tid]
            if t.status is TaskStatus.PENDING and all(
                    self.tasks[d].status is TaskStatus.COMPLETED
                    for d in t.dependencies if d in self.tasks):
                ready.append(t)
        return ready

    def mark_task_in_progress(self, tid: str) -> None:
        self.tasks[tid].status = TaskStatus.IN_PROGRESS

    def mark_task_complete(self, tid: str, result: str = "") -> None:
        self.tasks[tid].status = TaskStatus.COMPLETED
        self.tasks[tid].result = result

    def mark_task_failed(self, tid: str, reason: str) -> None:
        t = self.tasks[tid]
        t.failure_history.append(reason)
        t.retry_count += 1
        t.status = TaskStatus.FAILED if t.retry_count > t.max_retries else TaskStatus.PENDING

    def is_all_completed(self) -> bool:
        return bool(self.tasks) and all(t.status is TaskStatus.COMPLETED for t in self.tasks.values())

    def has_failed_tasks(self) -> bool:
        return any(t.status is TaskStatus.FAILED for t in self.tasks.values())

    def get_final_summary(self) -> str:
        if not self.tasks:
            return "Manager 没有创建可执行的任务。"
        completed = [t for t in self.tasks.values() if t.status is TaskStatus.COMPLETED]
        failed = [t for t in self.tasks.values() if t.status is TaskStatus.FAILED]
        lines = [f"已完成任务: {len(completed)}/{len(self.tasks)}"]
        for t in completed:
            lines.append(f"  [{t.id}] {t.description}")
            if t.result:
                lines += [f"      → {r}" for r in t.result.splitlines()]
        if failed:
            lines.append(f"失败任务: {len(failed)}")
            for t in failed:
                lines.append(f"  [{t.id}] {t.description}（重试 {t.retry_count} 次）")
                if t.failure_history:
                    lines.append(f"      最后失败原因: {t.failure_history[-1]}")
        return "\n".join(lines)


class _MessageBoard:
    """并行 Worker 共享消息板（单事件循环内，无需锁）。"""

    def __init__(self):
        self._messages: list[dict] = []

    async def post(self, worker_id: str, task_desc: str, message: str, status: str = "completed"):
        self._messages = [m for m in self._messages
                          if not (m["worker_id"] == worker_id and m["status"] != "completed")]
        self._messages.append({"worker_id": worker_id, "task": task_desc,
                               "message": message, "status": status})

    async def get_updates(self, exclude_worker: str | None = None) -> str:
        msgs = [m for m in self._messages if m["worker_id"] != exclude_worker]
        if not msgs:
            return ""
        return "\n".join(
            f"{'✅' if m['status'] == 'completed' else '🔄'} [{m['worker_id']}] {m['task']}\n   结果: {m['message']}"
            for m in msgs)


class _BoardTools:
    def __init__(self, board: _MessageBoard, worker_id: str, task_desc: str):
        self._board = board
        self._worker_id = worker_id
        self._task_desc = task_desc

    async def check_other_workers_progress(self) -> str:
        """查看其他并行 Worker 的进展与结果，避免重复劳动或基于其产出继续。"""
        return await self._board.get_updates(exclude_worker=self._worker_id) or "其他 Worker 暂无进展。"

    async def report_progress(self, message: str) -> str:
        """向共享消息板通报你的当前进展，供其他并行 Worker 参考。
        Parameters:
            message: 进展摘要
        """
        await self._board.post(self._worker_id, self._task_desc, message, status="in_progress")
        return "已通报进展。"


def _is_success(output: str) -> bool:
    return output.lstrip().startswith("SUCCESS:")


class WorkerOrchestrator:
    """Worker 执行编排：单任务（adhoc）与依赖分波并行。每个 Worker 复制独立 gf 状态文件。"""

    def __init__(self, *, task_manager: TaskManager, worker_model, workdir: Path,
                 make_tools, skills_manager):
        self._tm = task_manager
        self._worker_model = worker_model
        self._workdir = Path(workdir)
        self._make_tools = make_tools  # (state_file: Path) -> list[tool]
        self._skills_manager = skills_manager
        self._adhoc_seq = 0

    def _spawn_state(self, label) -> Path:
        main = self._workdir / "cli.json"
        state = self._workdir / f"worker_{label}_cli.json"
        if main.exists():
            shutil.copyfile(main, state)
        return state

    async def execute_task_with_worker(self, task_description: str, user_goal: str = "",
                                       retry_info: str = "") -> tuple[bool, str]:
        self._adhoc_seq += 1
        state = self._spawn_state(f"adhoc_{self._adhoc_seq}")
        agent = create_agent(self._worker_model, self._make_tools(state),
                             get_worker_system_prompt(self._skills_manager))
        prompt = f"[用户最终目标]\n{user_goal}\n\n[当前任务]\n{task_description}"
        if retry_info:
            prompt += f"\n\n这是重试。上次失败详情：\n{retry_info}\n请换一种方式完成。"
        token = ROLE.set(f"worker_adhoc_{self._adhoc_seq}")
        try:
            result = await agent.run(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return False, f"执行异常: {e}"
        finally:
            ROLE.reset(token)
        output = str(result.output or "")
        return _is_success(output), output

    async def execute_all_tasks_parallel(self, user_goal: str) -> str:
        board = _MessageBoard()
        for _wave in range(MAX_WAVES):
            ready = self._tm.get_all_ready_tasks()
            if not ready:
                break
            for t in ready:
                self._tm.mark_task_in_progress(t.id)
            sem = asyncio.Semaphore(MAX_WORKER_CONCURRENT)

            async def _run_one(task: Task):
                async with sem:
                    return await self._run_board_worker(task, board, user_goal)

            results = await asyncio.gather(*[_run_one(t) for t in ready], return_exceptions=True)
            for t, r in zip(ready, results):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, BaseException):
                    self._tm.mark_task_failed(t.id, f"执行异常: {r}")
                else:
                    success, output = r
                    if success:
                        self._tm.mark_task_complete(t.id, output)
                    else:
                        self._tm.mark_task_failed(t.id, output)
        return self._tm.get_final_summary()

    async def _run_board_worker(self, task: Task, board: _MessageBoard,
                                user_goal: str) -> tuple[bool, str]:
        worker_id = f"worker_{task.id}"
        state = self._spawn_state(task.id)
        tools = self._make_tools(state)
        bt = _BoardTools(board, worker_id, task.description)
        tools = tools + [bt.check_other_workers_progress, bt.report_progress]
        agent = create_agent(self._worker_model, tools,
                             get_worker_system_prompt(self._skills_manager, parallel=True))

        parts = [f"[用户最终目标]\n{user_goal}\n"]
        dep_parts = [f"[任务 {d}: {self._tm.tasks[d].description}]\n{self._tm.tasks[d].result}"
                     for d in task.dependencies
                     if d in self._tm.tasks and self._tm.tasks[d].status is TaskStatus.COMPLETED
                     and self._tm.tasks[d].result]
        if dep_parts:
            parts.append("[前置任务结果]\n" + "\n---\n".join(dep_parts) + "\n")
        updates = await board.get_updates(exclude_worker=worker_id)
        if updates:
            parts.append(f"[其他 Worker 进展]\n{updates}\n")
        parts.append(f"[当前任务]\n{task.description}")
        if task.retry_count:
            fails = "\n".join(f"  第{i + 1}次: {r}" for i, r in enumerate(task.failure_history))
            parts.append(f"\n这是第 {task.retry_count} 次重试。此前失败：\n{fails}\n请换一种方式。")
        prompt = "\n".join(parts)

        token = ROLE.set(worker_id)
        try:
            result = await agent.run(prompt, message_history=task.history)
        finally:
            ROLE.reset(token)
        task.history = result.all_messages()
        output = str(result.output or "")
        success = _is_success(output)
        await board.post(worker_id, task.description, output,
                         "completed" if success else "failed")
        return success, output
