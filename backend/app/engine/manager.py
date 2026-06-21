import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine.runner import execute_run
from app.models import Run, User


class RunManager:
    """运行任务的进程内登记处：取消事件、用户信号量、后台 asyncio.Task。"""

    def __init__(self):
        self.user_sems: dict[int, tuple[int, asyncio.Semaphore]] = {}
        self.cancel_events: dict[int, asyncio.Event] = {}
        self.done_events: dict[int, asyncio.Event] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    def user_sem(self, user_id: int, capacity: int) -> asyncio.Semaphore:
        # 连同容量缓存：容量变化时重建信号量，使 max_llm_concurrency 配置变更下一次提交即生效
        # （原实现首建即冻结，改并发度须重启进程才生效）；同容量复用，保持单一并发上限。
        cached = self.user_sems.get(user_id)
        if cached is None or cached[0] != capacity:
            self.user_sems[user_id] = (capacity, asyncio.Semaphore(capacity))
        return self.user_sems[user_id][1]

    def submit(self, run_id: int, user_id: int, capacity: int,
               session_factory: async_sessionmaker) -> None:
        ev = asyncio.Event()
        done = asyncio.Event()
        self.cancel_events[run_id] = ev
        self.done_events[run_id] = done
        task = asyncio.create_task(
            execute_run(run_id, session_factory, self.user_sem(user_id, capacity), ev))
        self.tasks[run_id] = task

        def _on_done(_):
            done.set()
            self._cleanup(run_id)
        task.add_done_callback(_on_done)

    def _cleanup(self, run_id: int) -> None:
        self.cancel_events.pop(run_id, None)
        self.done_events.pop(run_id, None)
        self.tasks.pop(run_id, None)

    def cancel(self, run_id: int) -> None:
        ev = self.cancel_events.get(run_id)
        if ev:
            ev.set()

    async def wait(self, run_id: int) -> None:
        """等待某次运行到达终态。未知/已结束 run 立即返回。"""
        done = self.done_events.get(run_id)
        if done is not None:
            await done.wait()


manager = RunManager()


async def resume_unfinished(session_factory: async_sessionmaker) -> int:
    """进程启动时恢复 queued/running 的运行（断点续跑）。返回恢复数量。"""
    async with session_factory() as s:
        rows = (await s.execute(
            select(Run, User).join(User, Run.user_id == User.id)
            .where(Run.status.in_(("queued", "running")))
        )).all()
    for run, user in rows:
        manager.submit(run.id, user.id, user.max_llm_concurrency, session_factory)
    return len(rows)
